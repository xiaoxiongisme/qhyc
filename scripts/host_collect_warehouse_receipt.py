"""
宿主机侧仓单采集器（DCE/GFEX）。

背景：云端容器无法下载无头 Chromium（CDN 受限），且按原架构 DCE/GFEX 仓单本就由
「宿主机采集器」负责。本脚本在你本地机器（Windows/macOS/Linux）运行，使用本机已安装的
patchright chromium 抓 DCE/GFEX，并通过 SSH 隧道把数据写入云端库。
云端调度器只负责 SHFE/CZCE（scrapling Fetcher，无需浏览器）。

前置条件：
  1. 本机已建立 SSH 隧道：本地 15432 -> 云端 timescaledb:5432
  2. pip install "scrapling[fetchers]" && patchright install chromium
  3. 设置环境变量 QHYC_CLOUD_DB_URL=postgresql://futures:<pwd>@127.0.0.1:15432/futures
     （未设置时回退到默认隧道地址；也可用 DATABASE_URL 覆盖）

运行示例：
  python scripts/host_collect_warehouse_receipt.py                 # 默认 DCE+GFEX，抓昨天
  python scripts/host_collect_warehouse_receipt.py --start 2026-09-25 --end 2026-09-30
  python scripts/host_collect_warehouse_receipt.py --exchange all  # 四所都跑（SHFE/CZCE 幂等覆盖）
日志：logs/warehouse_receipt_host.log（同时输出控制台）
"""
import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://futures:qhyc_dev_pwd_2026@127.0.0.1:15432/futures"


def _setup_db_url() -> None:
    url = os.environ.get("QHYC_CLOUD_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        url = DEFAULT_DB_URL
        print(f"[host-collector] 未设置 QHYC_CLOUD_DB_URL，回退默认隧道地址 {DEFAULT_DB_URL}")
    os.environ["DATABASE_URL"] = url


def _setup_logging() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "warehouse_receipt_host.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    logging.getLogger().addHandler(logging.StreamHandler())


def main() -> int:
    _setup_logging()
    _setup_db_url()
    # 默认只跑 DCE/GFEX（云端已负责 SHFE/CZCE），命令行可覆盖
    if "--exchange" not in sys.argv:
        sys.argv += ["--exchange", "DCE", "GFEX"]
    sys.path.insert(0, str(ROOT / "scripts"))
    import collect_warehouse_receipt as wr
    logging.info("宿主机仓单采集器启动，目标 DB=%s", os.environ["DATABASE_URL"])
    return wr.main()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
