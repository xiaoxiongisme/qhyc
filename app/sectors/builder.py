"""
§16.1 大类指数与传导权重构建（M2 排期：静态先验层）

- sync_sector_map：transmission_config.yaml → sector_map（幂等）
- build_sector_index：板块内品种日收益按权重合成日度指数
  · 权重方法：amount（成交额）缺失 → volume（成交量）回退，可配置 equal
    （实测 akshare futures_main_sina 无成交额列，daily_bar.amount 全空）
  · 成分每日留痕 composition JSON（§16.7 ④）
  · 动量：指数水平链式累乘，ret_5d/ret_20d = 区间累计
- sync_prior_weights：chains 先验 → transmission_weights（method=prior）
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import yaml
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT
from app.core.logging import logger
from app.models import DailyBar, SectorIndex, SectorMap, TransmissionWeight

CONFIG_PATH = PROJECT_ROOT / "config" / "transmission_config.yaml"


def load_transmission_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sync_sector_map(session: Session) -> int:
    """yaml 分类 → sector_map（幂等 upsert）"""
    cfg = load_transmission_config()
    rows = []
    for sector, spec in (cfg.get("sectors") or {}).items():
        products = spec.get("products") or []
        roles = spec.get("roles") or {}
        predict_enabled = spec.get("predict_enabled", True)
        for p in products:
            rows.append(
                {
                    "product": p.upper(),
                    "sector": sector,
                    "chain_role": roles.get(p),
                    # active = 纳入预测范围（金融期货 predict_enabled=false）
                    "active": bool(predict_enabled),
                    "src": "yaml",
                }
            )
    if not rows:
        return 0
    stmt = pg_insert(SectorMap).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["product"],
        set_={
            "sector": stmt.excluded.sector,
            "chain_role": stmt.excluded.chain_role,
            "active": stmt.excluded.active,
            "src": "yaml",
            "updated_at": text("NOW()"),
        },
    )
    session.execute(stmt)
    session.commit()
    logger.info(f"[sector] sector_map synced {len(rows)} products")
    return len(rows)


def sector_of(session: Session, product: str) -> str | None:
    row = session.execute(
        select(SectorMap.sector).where(SectorMap.product == product.upper())
    ).first()
    return row[0] if row else None


def predict_enabled_products(session: Session) -> list[str]:
    """纳入预测范围的品种（§16.1）

    仅排除 sector_map 中显式 active=false 的品种（金融期货）；
    未分类品种默认允许预测。比较按 product（IF）而非 symbol（IF888）。
    """
    from app.core.config import get_settings

    rows = session.execute(select(SectorMap.product, SectorMap.active)).all()
    excluded = {p for p, a in rows if not a}
    return [
        s.symbol
        for s in get_settings().main_contracts
        if s.product not in excluded
    ]


def build_sector_index(session: Session, start: str | None = None, end: str | None = None) -> dict:
    """合成大类指数（全量回补或增量）

    收益率口径：daily_bar.ret_close（各品种主连连续价；板块层面跳空互抵，M3 动态层再统一平滑口径）
    """
    cfg = load_transmission_config()
    icfg = cfg.get("index") or {}
    method = icfg.get("weight_method", "volume")
    base_level = float(icfg.get("base_level", 1000))
    windows = icfg.get("momentum_windows", [5, 20])
    min_members = int(icfg.get("min_members", 3))

    # 1. 板块成员
    members: dict[str, list[str]] = {}
    for row in session.execute(select(SectorMap)).scalars().all():
        members.setdefault(row.sector, []).append(row.product)

    stats: dict[str, int] = {}
    for sector, products in members.items():
        if len(products) < min_members:
            logger.info(f"[sector] {sector} 成员 {len(products)} < {min_members}，跳过指数")
            continue
        syms = [f"{p}888" for p in products]

        # 2. 板块内各品种收益/权重序列
        frames = []
        for sym in syms:
            rows = session.execute(
                select(
                    DailyBar.trade_date,
                    DailyBar.ret_close,
                    DailyBar.volume,
                    DailyBar.amount,
                )
                .where(
                    DailyBar.symbol == sym,
                    DailyBar.ret_close.is_not(None),
                    *([DailyBar.trade_date >= date.fromisoformat(start)] if start else []),
                    *([DailyBar.trade_date <= date.fromisoformat(end)] if end else []),
                )
                .order_by(DailyBar.trade_date)
            ).all()
            if not rows:
                continue
            frames.append(
                pd.DataFrame(
                    [r for r in rows],
                    columns=["trade_date", "ret", "volume", "amount"],
                ).set_index("trade_date").assign(product=sym[:-3])
            )
        if not frames:
            stats[sector] = 0
            continue

        big = pd.concat(frames)  # long: trade_date, ret, volume, amount, product
        # 3. 每日权重（amount 缺失 → volume 回退）
        if method == "amount" and big["amount"].notna().sum() > len(big) * 0.5:
            wcol = "amount"
            used = "amount"
        elif method == "equal":
            wcol = None
            used = "equal"
        else:
            wcol = "volume"
            used = "volume" if method != "equal" else "equal"

        # 收益矩阵
        ret_m = big.pivot_table(index="trade_date", columns="product", values="ret")
        ret_m = ret_m.sort_index()

        # 权重矩阵（每日归一）
        if used == "equal":
            w_m = ret_m.notna().astype(float)
        else:
            w_m = big.pivot_table(index="trade_date", columns="product", values=wcol)
            w_m = w_m.reindex(ret_m.index).reindex(ret_m.columns, axis=1).fillna(0.0)
        w_sum = w_m.sum(axis=1)
        w_m = w_m.div(w_sum.replace(0, np_nan_guard()), axis=0)

        # 4. 板块日收益 = Σ w_i × ret_i（缺数据品种权重自动归一到在场品种）
        valid = ret_m.notna() & (w_m > 0)
        wm = w_m.where(valid, 0.0)
        wm = wm.div(wm.sum(axis=1).replace(0, np_nan_guard()), axis=0)
        sector_ret = (wm * ret_m.fillna(0)).sum(axis=1)
        sector_ret = sector_ret[wm.sum(axis=1) > 0]

        # 5. 指数水平（链式累乘）+ 动量
        level = base_level * (1 + sector_ret / 100.0).cumprod()
        out = pd.DataFrame({"ret_1d": sector_ret, "index_level": level})
        for w in windows:
            cum = (1 + sector_ret / 100.0).rolling(w).apply(np.prod, raw=True) - 1
            out[f"ret_{w}d"] = cum * 100.0

        # 6. 落库（成分留痕）
        rows = []
        for d, r in out.iterrows():
            comp = {
                p: round(float(wm.loc[d, p]), 6)
                for p in ret_m.columns
                if valid.loc[d].get(p, False) and wm.loc[d, p] > 0
            }
            rows.append(
                {
                    "sector": sector,
                    "trade_date": d,
                    "ret_1d": round(float(r["ret_1d"]), 6),
                    **{f"ret_{w}d": (round(float(r[f"ret_{w}d"]), 6) if pd.notna(r[f"ret_{w}d"]) else None) for w in windows},
                    "index_level": round(float(r["index_level"]), 6),
                    "weight_method": used,
                    "composition": comp,
                }
            )
        if rows:
            stmt = pg_insert(SectorIndex).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["sector", "trade_date"],
                set_={
                    "ret_1d": stmt.excluded.ret_1d,
                    "ret_5d": stmt.excluded.ret_5d,
                    "ret_20d": stmt.excluded.ret_20d,
                    "index_level": stmt.excluded.index_level,
                    "weight_method": stmt.excluded.weight_method,
                    "composition": stmt.excluded.composition,
                },
            )
            session.execute(stmt)
        stats[sector] = len(rows)
        logger.info(f"[sector] {sector} 指数写入 {len(rows)} 天（权重法={used}）")

    session.commit()
    logger.info(f"[sector] sector_index done: {stats}")
    return stats


def np_nan_guard() -> float:
    """避免除零的极小占位（权重和为 0 的日期会被过滤）"""
    return 1e-12


def sync_prior_weights(session: Session) -> int:
    """chains 先验 → transmission_weights（method=prior，§16.7 ⑤：每周重算由 M3 动态层接管）"""
    cfg = load_transmission_config()
    chains = cfg.get("chains") or []
    rows = []
    for c in chains:
        rows.append(
            {
                "src_product": str(c["src"]).upper(),
                "dst_product": str(c["dst"]).upper(),
                "method": "prior",
                "lag_days": int(c.get("lag_days", 1)),
                "direction": c.get("direction", "positive"),
                "weight": float(c.get("strength", 0.5)),
                "cost_ratio": (
                    float(c["cost_ratio"]) if c.get("cost_ratio") is not None else None
                ),
            }
        )
    if not rows:
        return 0
    stmt = pg_insert(TransmissionWeight).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["src_product", "dst_product", "method", "lag_days"],
        set_={
            "direction": stmt.excluded.direction,
            "weight": stmt.excluded.weight,
            "cost_ratio": stmt.excluded.cost_ratio,
            "updated_at": text("NOW()"),
        },
    )
    session.execute(stmt)
    session.commit()
    logger.info(f"[sector] transmission_weights(prior) synced {len(rows)} chains")
    return len(rows)