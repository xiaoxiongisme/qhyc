"""启动快照（copy-on-start）+ md5 manifest（PRD §5.3）。

容器启动时把**只读挂载**的 WB 源码整份复制到容器内可写目录，之后所有子进程跑这份副本：
- 消除「宿主正在编辑 → 容器读到半截文件」
- 消除跨 OS 文件锁
- manifest 落库，作为「单一真源」的审计凭据

⚠ 分工铁律：这里改的是**快照副本**，WB skill 目录源文件一个字节都不动。
   唯一的例外是下面 `_patch_base()`：把 `run_pipeline.py` 里硬编码的
   `BASE = r"E:/QH/期货简报"` 改成 env 优先读取（PRD §5.2② 属 WB 的 P0 项；
   WB 未改动前，我们在副本上打补丁，保证容器可跑且不污染真源）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from app.core.logging import logger

# 硬编码 BASE → env 优先（与 PRD §5.2② 的 WB 侧改造等价）
_BASE_OLD = 'BASE = r"E:/QH/期货简报"'
_BASE_NEW = 'BASE = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")'


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _patch_base(dest: Path) -> bool:
    """把快照副本里的硬编码 BASE 改成 env 优先。返回是否实际打了补丁。"""
    target = dest / "run_pipeline.py"
    if not target.exists():
        return False
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return False
    if _BASE_OLD not in text:
        return False  # WB 已改造或写法变化 → 不动
    if "import os" not in text:
        lines = text.splitlines()
        insert_at = 0
        for i, ln in enumerate(lines[:40]):
            if ln.startswith("import ") or ln.startswith("from "):
                insert_at = i + 1
        lines.insert(insert_at, "import os")
        text = "\n".join(lines) + "\n"
    text = text.replace(_BASE_OLD, _BASE_NEW)
    target.write_text(text, encoding="utf-8")
    logger.info("[pipeline] snapshot: 已在副本上把 run_pipeline.BASE 改为 QH_BRIEF_BASE 优先")
    return True


def _file_index(root: Path) -> dict[str, str]:
    """相对路径 -> md5（排除 __pycache__ 与隐藏路径）。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out[rel.as_posix()] = _md5(p)
    return out


def needs_resync(src: str, dest: str) -> bool:
    """判断快照是否需要重建。

    ⚠ 历史 bug：调用方（worker.ensure_snapshot）过去只在「快照目录为空」时同步，
    于是：一旦建立过快照，① WB 源码后续更新永不生效；② 更严重的是，本文件新增的
    `_patch_base()` 逻辑对**已存在的旧快照**永远不会执行 —— run_pipeline.py 里
    仍是硬编码 `BASE = r"E:/QH/期货简报"`，在 Linux 容器里被当成相对路径，
    产出被写到 `/app/runtime/pipeline_snapshot/E:/QH/...`（一堆垃圾目录），
    宿主简报目录反而没更新。表现为 /pipeline/status 的 base_patch_applied=false。

    判定规则（任一命中即重建）：
      1. 快照不存在 / 为空
      2. 快照里的 run_pipeline.py 仍未打 BASE 补丁
      3. 源文件集合或内容发生变化
         （run_pipeline.py 例外：以「是否已打补丁」判断，不比 md5，否则永不收敛）
    """
    src_p, dest_p = Path(src), Path(dest)
    if not src_p.exists():
        # 源不可见（bind mount 丢失 / 路径写错）→ 保留旧快照，绝不把空目录覆盖进去
        logger.warning(f"[pipeline] snapshot: 源不可见 {src}，跳过重建（保留现有快照）")
        return False
    if not dest_p.exists() or not any(dest_p.iterdir()):
        return True

    rp = dest_p / "run_pipeline.py"
    try:
        if rp.exists() and _BASE_OLD in rp.read_text(encoding="utf-8", errors="replace"):
            logger.info("[pipeline] snapshot: 检测到 BASE 补丁未生效，重建快照")
            return True
    except Exception:  # noqa: BLE001
        pass

    s, d = _file_index(src_p), _file_index(dest_p)
    if set(s) != set(d):
        return True
    for k, v in s.items():
        if k == "run_pipeline.py":
            continue
        if d.get(k) != v:
            return True
    return False


def sync(src: str, dest: str) -> tuple[list[dict], bool]:
    """把 src 复制为 dest 快照，返回 (manifest, base_patched)。

    manifest = [{"name": 相对路径, "md5": ...}, ...]（排序稳定，便于比对）
    """
    src_p, dest_p = Path(src), Path(dest)
    if not src_p.exists():
        raise FileNotFoundError(f"pipeline 源码挂载不可见: {src}")
    tmp = Path(str(dest_p) + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src_p, tmp, dirs_exist_ok=True)
    patched = _patch_base(tmp)
    if dest_p.exists():
        shutil.rmtree(dest_p)
    os.replace(tmp, dest_p)

    manifest = [
        {"name": str(p.relative_to(dest_p)).replace("\\", "/"), "md5": _md5(p)}
        for p in sorted(dest_p.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    logger.info(f"[pipeline] snapshot ok {src} -> {dest} files={len(manifest)} patched={patched}")
    return manifest, patched


def manifest_of(dest: str) -> list[dict]:
    """读取当前快照的 md5 清单（供 /pipeline/manifest 做落后校验）。"""
    dest_p = Path(dest)
    if not dest_p.exists():
        return []
    return [
        {"name": str(p.relative_to(dest_p)).replace("\\", "/"), "md5": _md5(p)}
        for p in sorted(dest_p.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]


def manifest_digest(manifest: list[dict]) -> str:
    """整份清单的稳定摘要（落库比对用）。"""
    h = hashlib.md5()
    for item in manifest:
        h.update(f"{item['name']}:{item['md5']}\n".encode("utf-8"))
    return h.hexdigest()
