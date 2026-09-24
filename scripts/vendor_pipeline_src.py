"""把 WB 决策链路源码「内化」到 vendor/pipeline_src，供镜像构建 COPY。

为什么需要这一步
----------------
本地 docker-compose 用 `${WB_PIPELINE_PATH}` bind mount 把
`C:/Users/Seven/.workbuddy/skills/futures-daily-brief-pipeline/scripts` 挂进容器
`/app/pipeline_src`。远端（腾讯云轻量服务器）不存在该路径 → Docker 会静默建一个空目录，
决策链路（简报/研究/基本面/盘中监控/信号）无源码可执行，且不会报错。

解法：构建前把源码快照进仓库内的 `vendor/pipeline_src/`，Dockerfile 直接 COPY 进镜像。
镜像因此自足，远端无需任何宿主机路径挂载。

⚠ 铁律：只**复制** WB 源文件，不修改、不回写 WB 目录一个字节。

用法
----
    python scripts/vendor_pipeline_src.py                      # 用默认/环境变量路径
    python scripts/vendor_pipeline_src.py --src <path>         # 显式指定
    python scripts/vendor_pipeline_src.py --check              # 只校验是否与源一致
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEST = ROOT / "vendor" / "pipeline_src"

DEFAULT_SRC = os.environ.get(
    "WB_PIPELINE_PATH",
    r"C:/Users/Seven/.workbuddy/skills/futures-daily-brief-pipeline/scripts",
)

MANIFEST_NAME = "_vendor_manifest.txt"

EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp"}


def _iter_files(src: Path):
    """遍历源文件。

    排除规则：
      - 显式目录（__pycache__ / .git / ...）
      - **所有以 `.` 开头的路径段**：WB skill 目录下带 `.workbuddy/memory/`
        等私有记忆与自动化状态文件，与运行无关且可能含敏感内容，绝不进镜像。
    """
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(src).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix in EXCLUDE_SUFFIX:
            continue
        if p.name == MANIFEST_NAME:  # 本脚本自己生成的清单，不是源文件
            continue
        yield p


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="内化 WB 决策链路源码到 vendor/pipeline_src")
    ap.add_argument("--src", default=DEFAULT_SRC, help="WB scripts 目录")
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--check", action="store_true",
                    help="只校验 vendor 副本与源是否一致（用于 CI / 构建前自检）")
    a = ap.parse_args()

    src, dest = Path(a.src), Path(a.dest)
    if not src.exists():
        print(f"❌ 源目录不存在: {src}")
        print("   远端部署请先在**开发机**执行本脚本生成 vendor/pipeline_src，再构建镜像。")
        return 2

    files = list(_iter_files(src))
    if not files:
        print(f"❌ 源目录为空: {src}")
        return 2

    if a.check:
        if not dest.exists():
            print(f"❌ vendor 副本不存在: {dest}")
            return 1
        diffs = []
        for f in files:
            rel = f.relative_to(src)
            d = dest / rel
            if not d.exists():
                diffs.append(f"缺少 {rel}")
            elif _md5(f) != _md5(d):
                diffs.append(f"内容不同 {rel}")
        extra = [str(p.relative_to(dest)) for p in _iter_files(dest)
                 if not (src / p.relative_to(dest)).exists()]
        if diffs or extra:
            print(f"❌ 不一致 {len(diffs)} 项，多余 {len(extra)} 项：")
            for x in (diffs + extra)[:20]:
                print("   ", x)
            return 1
        print(f"✅ vendor 副本与源一致（{len(files)} 个文件）")
        return 0

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    for f in files:
        rel = f.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
        n += 1
        total += f.stat().st_size

    manifest = dest / MANIFEST_NAME
    manifest.write_text(
        "\n".join(f"{f.relative_to(src).as_posix()}  {_md5(f)}" for f in files) + "\n",
        encoding="utf-8",
    )
    print(f"✅ 已内化 {n} 个文件 / {total/1024:.0f} KB  ->  {dest}")
    print(f"   源: {src}")
    print(f"   清单: {manifest.name}（构建前可用 --check 校验是否漂移）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
