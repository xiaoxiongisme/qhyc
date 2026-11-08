# -*- coding: utf-8 -*-
"""回测 / 实盘共用的执行契约层（零副作用）。

⚠ 本包 ``__init__`` 必须保持为空：``app.backtest.fusion_backtest``（回测）、
``scripts.ops.guard_invariants``（守卫）与未来的实盘影子对账都要 import 它，
任何 DB / 配置 / ORM 的导入期副作用都会造成循环依赖或拖慢每日 09:10 守卫。

刻意不放进 ``app/execution/``：那个包的 ``__init__`` 会连带 service、
lot_sizing、reverse_price、roll_policy、validator 五个子模块（实测 0.861s），
与「窄接口、不动实盘」的 E1 结论相悖。具体契约见 ``app.contracts.execution``。
"""
