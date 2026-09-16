# -*- coding: utf-8 -*-
"""futures-data-fetch 适配层（PostgreSQL 版）。

把技能的 DolphinDB 落库层替换为本项目 TimescaleDB（db_pg）。
使用：python -m app.ingest.fdf.fetch_fdf --freq hourly
      python -m app.ingest.fdf.adjust_fdf CZCE.FG --freq hourly
"""
