"""CL-20261110-14：压缩脚本日志路径与「失败可冒泡」契约回归。

背景：run_compress.sh 曾硬编码 ``>> /var/log/qhyc_compress.log``，而 /var/log
仅 root 可写，靠 sudo 预创建 + chown 才不报 exit 126；且 psql -f 不带
ON_ERROR_STOP，SQL 全失败仍退 0，task_runner 按 returncode 记 success（假绿）。
这些契约必须由可重复执行的测试守住，而不是靠人记得。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/db/run_compress.sh"
DEFAULT_LOG_DIR = "/home/ubuntu/qhyc_work/logs"


def _code_lines() -> str:
    """只取有效代码行——注释里允许出现 /var/log 作为历史说明。"""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return "\n".join(x for x in lines if x.strip() and not x.lstrip().startswith("#"))


def test_no_var_log_in_effective_code():
    assert "/var/log" not in _code_lines()


def test_default_log_dir_is_project_writable_dir():
    code = _code_lines()
    assert f'LOG_DIR="${{QHYC_LOG_DIR:-{DEFAULT_LOG_DIR}}}"' in code
    probe = subprocess.run(
        ["sh", "-c", f'LOG_DIR="${{QHYC_LOG_DIR:-{DEFAULT_LOG_DIR}}}"; printf "%s" "$LOG_DIR"'],
        capture_output=True, text=True, timeout=5)
    assert probe.stdout.strip() == DEFAULT_LOG_DIR


def test_shell_syntax_ok():
    if shutil.which("sh") is None:
        pytest.skip("sh 不可用")
    probe = subprocess.run(["sh", "-n", str(SCRIPT)],
                           capture_output=True, text=True, timeout=5)
    assert probe.returncode == 0, probe.stderr


def test_failure_not_swallowed():
    code = _code_lines()
    assert "ON_ERROR_STOP=1" in code
    assert 'exit "$rc"' in code
    assert "tail -n 30" in code  # 失败详情转发到 stderr，外层 task_runner.log 可见


def test_guard_assertion_passes():
    from scripts.ops.guard_invariants import Ctx, _c_compress_log_path

    class _R:
        def fetchall(self):
            return [("true",)]

    class _S:
        def execute(self, q, p=None):
            return _R()

    ok, _, observed, detail = _c_compress_log_path(Ctx(session=_S()))
    assert ok, f"{observed} / {detail}"
