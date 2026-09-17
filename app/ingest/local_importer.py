"""
本地历史数据导入器（§4.5，M1）

数据源目录：容器内 /app/imports （由 docker compose bind 挂载 E:/Docker/qhyc/imports） E:\\QH）
文件命名规范：
- {PRODUCT}_daily.json                  → main_continuous.raw_*
- {PRODUCT}_cont_adj.json               → main_continuous.adj_*
- {PRODUCT}_cont_adj_adjusted.csv       → hourly_bar（M6 主用，M1 也入库预留）
- {PRODUCT}_rolls.csv                   → main_contract_map（换月+delta）
- {PRODUCT}_contracts.json              → daily_bar（全合约，⑧）

示例：E:\\QH\\FG\\data\\E:/Docker/qhyc/imports/FG/data/FG_daily.json → product="FG"
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ImportError as QHYCImportError
from app.core.logging import logger
from app.ingest.utils import attach_returns
from app.repositories._base import (
    upsert_daily_bars,
    upsert_main_continuous,
)


# 品种 → 交易所映射（与 akshare 探测一致）
_PRODUCT_EXCHANGE = {
    "FG": "CZCE", "SA": "CZCE", "SR": "CZCE", "CF": "CZCE", "TA": "CZCE",
    "MA": "CZCE", "RM": "CZCE", "OI": "CZCE", "AP": "CZCE", "PK": "CZCE",
    "RB": "SHFE", "CU": "SHFE", "AU": "SHFE", "AG": "SHFE", "AL": "SHFE",
    "ZN": "SHFE", "PB": "SHFE", "SN": "SHFE", "NI": "SHFE", "SS": "SHFE",
    "FU": "SHFE", "BU": "SHFE",
    "M": "DCE", "Y": "DCE", "I": "DCE", "JM": "DCE", "J": "DCE", "P": "DCE",
    "C": "DCE", "A": "DCE", "B": "DCE", "L": "DCE", "V": "DCE", "PP": "DCE",
    "EG": "DCE", "EB": "DCE",
    "IF": "CFFEX", "IC": "CFFEX", "IH": "CFFEX", "T": "CFFEX", "TF": "CFFEX", "TS": "CFFEX",
    "SC": "INE", "NR": "INE", "LU": "INE", "BC": "INE",
}


class LocalHistoryImporter:
    """§4.5 CSV/JSON 模板导入器"""

    def __init__(self, session: Session):
        self.session = session
        s = get_settings()
        self.root = Path(s.yaml.imports.container_root or "/app/imports")
        self.source_root = s.yaml.imports.source_root  # 仅用于日志提示
        self.format_version = s.yaml.imports.format_version

    # ---------- 公开 API ----------
    def discover_products(self) -> list[str]:
        """按文件名前缀扫描品种（§4.5：{PRODUCT}_daily.json 等 5 类文件）

        兼容两种布局：
        - {root}/{P}/data/{P}_daily.json   （PRD 假设）
        - 多品种文件混放于任一 data 目录（实际数据：FG 与 SA 同放 FG/data）
        """
        if not self.root.exists():
            logger.warning(f"[import] 根目录不存在 {self.root}")
            return []
        products: set[str] = set()
        # 扫描深度限制：root/**/data/*，避免误扫无关目录
        pattern_daily = self.root.glob("**/data/*_daily.json")
        pattern_rolls = self.root.glob("**/data/*_rolls.csv")
        for p in list(pattern_daily) + list(pattern_rolls):
            m = re.match(r"^([A-Z]{1,4})_", p.name.upper())
            if m:
                products.add(m.group(1))
        return sorted(products)

    def _find_product_files(self, product: str) -> dict[str, Path]:
        """在 root 下所有 data 目录中定位某品种的 5 类模板文件"""
        found: dict[str, Path] = {}
        suffixes = {
            "daily": f"{product}_daily.json",
            "cont_adj": f"{product}_cont_adj.json",
            "hourly": f"{product}_cont_adj_adjusted.csv",
            "rolls": f"{product}_rolls.csv",
            "contracts": f"{product}_contracts.json",
        }
        for key, fname in suffixes.items():
            matches = sorted(self.root.glob(f"**/data/{fname}"))
            if matches:
                found[key] = matches[0]
        return found

    def import_product(self, product: str) -> dict:
        """导入单个品种的全部数据，返回统计"""
        product = product.upper()
        files = self._find_product_files(product)
        if not files:
            raise QHYCImportError(
                f"未找到 {product} 的 §4.5 模板文件（root={self.root}）"
            )
        logger.info(f"[import] {product} 文件定位: { {k: str(v) for k, v in files.items()} }")

        exchange = _PRODUCT_EXCHANGE.get(product, "UNKNOWN")
        stats: dict[str, Any] = {
            "product": product,
            "exchange": exchange,
            "daily_main_raw": 0,
            "daily_main_adj": 0,
            "rolls": 0,
            "contracts_bars": 0,
            "hourly": 0,
        }

        # 1. daily.json → main_continuous.raw_*
        if "daily" in files:
            stats["daily_main_raw"] = self._import_main_raw(
                product, exchange, files["daily"]
            )

        # 2. cont_adj.json → main_continuous.adj_*
        if "cont_adj" in files:
            stats["daily_main_adj"] = self._import_main_adj(
                product, exchange, files["cont_adj"]
            )

        # 3. rolls.csv → main_contract_map
        if "rolls" in files:
            stats["rolls"] = self._import_rolls(product, exchange, files["rolls"])

        # 4. contracts.json → daily_bar（⑧ 全合约）
        if "contracts" in files:
            stats["contracts_bars"] = self._import_contracts(
                product, exchange, files["contracts"]
            )

        # 5. cont_adj_adjusted.csv → hourly_bar（M6 主用，M1 入库预留）
        if "hourly" in files:
            stats["hourly"] = self._import_hourly(
                product, exchange, files["hourly"]
            )

        logger.info(f"[import] {product} -> {stats}")
        return stats

    def import_all(self) -> list[dict]:
        results = []
        for p in self.discover_products():
            try:
                results.append(self.import_product(p))
            except Exception as e:
                logger.exception(f"[import] {p} 失败: {e}")
                results.append({"product": p, "error": str(e)})
        return results

    # ---------- 各文件解析 ----------
    def _import_main_raw(self, product: str, exchange: str, path: Path) -> int:
        """{P}_daily.json → main_continuous.raw_*"""
        items = self._load_json_array(path)
        rows: list[dict] = []
        for it in items:
            td = self._parse_date(it.get("date"))
            if not td:
                continue
            rows.append(
                {
                    "product": product,
                    "trade_date": td,
                    "raw_open": self._num(it.get("open")),
                    "raw_high": self._num(it.get("high")),
                    "raw_low": self._num(it.get("low")),
                    "raw_close": self._num(it.get("close")),
                    "raw_volume": self._int(it.get("volume")),
                    "raw_oi": self._int(it.get("oi")),
                    "src": "csv",
                }
            )
        if not rows:
            return 0
        # main_continuous 表无 ret 字段；模型特征在 M2 特征层基于平滑主连现算，不在导入期落库
        n = upsert_main_continuous(self.session, rows)
        self.session.commit()
        logger.info(f"[import] {product} daily_main_raw upserted {n}")
        return n

    def _import_main_adj(self, product: str, exchange: str, path: Path) -> int:
        """{P}_cont_adj.json → main_continuous.adj_*"""
        items = self._load_json_array(path)
        rows: list[dict] = []
        for it in items:
            td = self._parse_date(it.get("date"))
            if not td:
                continue
            rows.append(
                {
                    "product": product,
                    "trade_date": td,
                    "adj_open": self._num(it.get("open")),
                    "adj_high": self._num(it.get("high")),
                    "adj_low": self._num(it.get("low")),
                    "adj_close": self._num(it.get("close")),
                    "adj_volume": self._int(it.get("volume")),
                    "adj_oi": self._int(it.get("oi")),
                    "src": "csv",
                }
            )
        if not rows:
            return 0
        n = upsert_main_continuous(self.session, rows)
        self.session.commit()
        logger.info(f"[import] {product} daily_main_adj upserted {n}")
        return n

    def _import_rolls(self, product: str, exchange: str, path: Path) -> int:
        """{P}_rolls.csv → main_contract_map（⑪ delta）"""
        rows: list[dict] = []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                td = self._parse_date(r.get("date"))
                if not td:
                    continue
                old = (r.get("old") or "").strip()
                new = (r.get("new") or "").strip()
                if not old or not new:
                    continue
                # main_symbol 用统一主连名 product + 888
                main_sym = f"{product}888"
                rows.append(
                    {
                        "trade_date": td,
                        "exchange": exchange,
                        "product": product,
                        "main_symbol": main_sym,
                        "underlying": new.split(".")[-1] if "." in new else new,
                        "change_flag": True,
                        "delta": self._num(r.get("delta")) or Decimal(0),
                        "src": "csv",
                    }
                )
        if not rows:
            return 0
        from app.repositories.main_contract_repo import MainContractRepository

        n = MainContractRepository(self.session).upsert(rows)
        self.session.commit()
        logger.info(f"[import] {product} rolls upserted {n}")
        return n

    def _import_contracts(self, product: str, exchange: str, path: Path) -> int:
        """{P}_contracts.json → daily_bar（⑧ 全合约）

        结构：{ "<exchange>.<contract_code>": [ {date, open, high, low, close, volume, oi}, ... ] }
        symbol 直接用 contract_code（如 FG010）
        """
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
        rows: list[dict] = []
        for full_code, items in data.items():
            # full_code 例 "CZCE.FG010" → symbol=FG010
            sym = full_code.split(".")[-1] if "." in full_code else full_code
            if not isinstance(items, list):
                continue
            for it in items:
                td = self._parse_date(it.get("date"))
                if not td:
                    continue
                rows.append(
                    {
                        "symbol": sym,
                        "trade_date": td,
                        "open": self._num(it.get("open")),
                        "high": self._num(it.get("high")),
                        "low": self._num(it.get("low")),
                        "close": self._num(it.get("close")),
                        "settle": None,
                        "volume": self._int(it.get("volume")),
                        "amount": None,
                        "oi": self._int(it.get("oi")),
                        "src": "csv",
                    }
                )
        if not rows:
            return 0
        rows = attach_returns(rows)
        n = upsert_daily_bars(self.session, rows)
        self.session.commit()
        logger.info(f"[import] {product} contracts upserted {n}")
        return n

    def _import_hourly(self, product: str, exchange: str, path: Path) -> int:
        """{P}_cont_adj_adjusted.csv → hourly_bar（M6 主用，M1 入库）"""
        rows: list[dict] = []
        main_sym = f"{product}888"
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                dt = self._parse_datetime(r.get("date"))
                if not dt:
                    continue
                close = self._num(r.get("close"))
                rows.append(
                    {
                        "symbol": main_sym,
                        "trade_datetime": dt,
                        "open": self._num(r.get("open")),
                        "high": self._num(r.get("high")),
                        "low": self._num(r.get("low")),
                        "close": close,
                        "volume": self._int(r.get("volume")),
                        "oi": None,
                        "src": "csv",
                    }
                )
        if not rows:
            return 0
        # hourly_bar 主键 (symbol, trade_datetime)，分块 upsert（psycopg 参数上限）
        from app.repositories._base import upsert_hourly_bars

        n = upsert_hourly_bars(self.session, rows)
        self.session.commit()
        logger.info(f"[import] {product} hourly upserted {n}")
        return n

    # ---------- helpers ----------
    def _load_json_array(self, path: Path) -> list[dict]:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return data["data"]
        return []

    def _parse_date(self, s: Any) -> date | None:
        if not s:
            return None
        if isinstance(s, date):
            return s
        if isinstance(s, datetime):
            return s.date()
        s = str(s)[:10]
        try:
            return date.fromisoformat(s)
        except Exception:
            return None

    def _parse_datetime(self, s: Any) -> datetime | None:
        if not s:
            return None
        if isinstance(s, datetime):
            return s
        s = str(s).strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    def _num(self, v) -> Decimal | None:
        if v is None or v == "":
            return None
        try:
            return Decimal(str(v))
        except Exception:
            return None

    def _int(self, v) -> int | None:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return None


__all__ = ["LocalHistoryImporter"]