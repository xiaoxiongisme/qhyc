"""一次性回填 hourly_bar 历史（用 tqsdk 取长历史，纠正此前缺失的下午盘/11:00/夜盘棒）。
用法：在 scheduler 容器内运行。日志写 stdout，建议重定向到 /app/logs/backfill.log。
"""
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill")

from app.core.db import get_session_factory
from app.ingest.hourly_collector import HourlyCollector


def main() -> None:
    t0 = time.time()
    s = get_session_factory()()
    c = HourlyCollector(s, prefer="tqsdk")
    logger.info("[backfill] start collect_all data_length=8000")
    res = c.collect_all(data_length=8000)
    c.close()
    s.close()
    ok = sum(1 for r in res if "error" not in r)
    errs = [r for r in res if "error" in r]
    logger.info(
        f"[backfill] DONE ok={ok}/{len(res)} elapsed={time.time() - t0:.0f}s"
    )
    for r in errs:
        logger.warning("ERR %s %s", r.get("symbol"), str(r.get("error"))[:200])


if __name__ == "__main__":
    main()
