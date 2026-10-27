# -*- coding: utf-8 -*-
"""风控外壳三层（P0 盘前门 / P1 离场 / P2 品种减法）的回归测试。

设计取向：**只测纯函数与判定边界**，不碰 DB。
理由：这套代码的价值全在"阈值算得对不对"，DB 读写只是搬运；
把断言压在算术上，任何口径漂移都会立刻红。

必测的三件事（对应 2026-10-26 事故的三个成因）：
1. 锁仓按交易所制度收单/双边 —— 郑商所双边、大商所单向大边
2. 保证金率百分数与小数两种口径都能归一
3. 数据缺失时判定只能是 UNKNOWN，**绝不退化为 GREEN**
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution import exit_guard as eg
from app.risk import margin_model as mm
from app.risk import variety_gate as vg


# ---------------------------------------------------------------------------
# 保证金模型
# ---------------------------------------------------------------------------
class TestNormalizeRate:
    @pytest.mark.parametrize("raw,expect", [
        (8.0, 0.08), (0.08, 0.08), (15.0, 0.15), (0.15, 0.15), (12.5, 0.125),
    ])
    def test_percent_and_decimal(self, raw, expect):
        assert mm.normalize_rate(raw) == pytest.approx(expect)

    def test_missing_returns_fallback_or_none(self):
        assert mm.normalize_rate(None) is None
        assert mm.normalize_rate(None, fallback=0.1) == 0.1
        assert mm.normalize_rate(0) is None
        assert mm.normalize_rate(-3) is None

    def test_garbage_returns_fallback(self):
        assert mm.normalize_rate("abc", fallback=0.2) == 0.2  # type: ignore[arg-type]


class TestLockPolicy:
    def test_czce_is_both_sides(self):
        assert mm.lock_policy("CZCE") == "BOTH_SIDES"
        assert mm.lock_policy("郑商所") == "BOTH_SIDES"

    @pytest.mark.parametrize("ex", ["SHFE", "DCE", "CFFEX", "GFEX", "INE",
                                    "上期所", "大商所", "中金所", "广期所"])
    def test_others_are_single_side(self, ex):
        assert mm.lock_policy(ex) == "SINGLE_SIDE"

    def test_unknown_exchange_falls_back_to_both_sides(self):
        """未知交易所必须按"多收"处理：少收会直接导致盘中追保。"""
        assert mm.lock_policy(None) == "BOTH_SIDES"
        assert mm.lock_policy("火星交易所") == "BOTH_SIDES"


class TestScenarioUsage:
    def _legs(self):
        return [
            mm.PositionLeg("MA701", "CZCE", long_lots=4, short_lots=4,
                           price=3272.0, multiplier=10.0),
            mm.PositionLeg("EG2701", "DCE", long_lots=2, short_lots=2,
                           price=5032.0, multiplier=10.0),
        ]

    def _rules(self):
        return {
            "MA701": mm.RuleRow(margin_ratio=8.0, price_limit=5.0, multiplier=10.0),
            "EG2701": mm.RuleRow(margin_ratio=10.0, price_limit=7.0, multiplier=10.0),
        }

    def test_czce_lock_charges_both_sides(self):
        """MA701 多4空4：郑商所双边 → 8 手全收。"""
        leg = self._legs()[0]
        amt, why = mm.leg_margin(leg, self._rules()["MA701"])
        assert why is None
        assert amt == pytest.approx(8 * 10 * 3272 * 0.08)

    def test_dce_lock_charges_larger_side_only(self):
        """EG2701 多2空2：大商所单向大边 → 只收 2 手。"""
        leg = self._legs()[1]
        amt, why = mm.leg_margin(leg, self._rules()["EG2701"])
        assert why is None
        assert amt == pytest.approx(2 * 10 * 5032 * 0.10)

    def test_gross_vs_net_notional(self):
        """锁仓时净名义为 0，毛名义不为 0——这正是事故里被忽略的量。"""
        r = mm.scenario_usage(self._legs(), self._rules(), 100000)
        assert r.net_notional == pytest.approx(0.0)
        assert r.gross_notional > 0

    def test_zero_equity_raises(self):
        with pytest.raises(ValueError, match="equity"):
            mm.scenario_usage(self._legs(), self._rules(), 0)

    def test_missing_rule_reported_not_silent(self):
        r = mm.scenario_usage(self._legs(), {}, 100000)
        assert r.missing, "缺规则必须进 missing 列表，不能静默当 0"
        assert r.margin_required == 0.0

    def test_extreme_scenario_exceeds_normal(self):
        now = mm.scenario_usage(self._legs(), self._rules(), 100000, rate_buffer=1.0)
        ext = mm.scenario_usage(self._legs(), self._rules(), 100000,
                                rate_buffer=1.5, limit_shock=1.0)
        assert ext.usage > now.usage * 1.4


class TestVerdict:
    def test_ten_pct_equity_is_red(self):
        v, why = mm.verdict_from_usage(1.10, 1.60)
        assert v == "RED" and len(why) == 2

    def test_healthy_is_green(self):
        v, why = mm.verdict_from_usage(0.30, 0.50)
        assert v == "GREEN" and not why

    def test_between_warn_and_stop_is_yellow(self):
        v, _ = mm.verdict_from_usage(0.45, 0.50)
        assert v == "YELLOW"

    def test_worst_prefers_unknown(self):
        assert mm.worst("GREEN", "UNKNOWN") == "UNKNOWN"
        assert mm.worst("YELLOW", "RED") == "RED"
        assert mm.worst() == "GREEN"


class TestLimitRoom:
    def test_room_at_settle(self):
        r = mm.limit_room(3400, 3400, mm.RuleRow(price_limit=5.0))
        assert r is not None
        assert r["to_up_pct"] == pytest.approx(0.05)

    def test_missing_price_limit(self):
        assert mm.limit_room(3400, 3400, mm.RuleRow()) is None


# ---------------------------------------------------------------------------
# 离场
# ---------------------------------------------------------------------------
class TestSplitLots:
    @pytest.mark.parametrize("lots,expect_sum", [(1, 1), (2, 2), (3, 3), (4, 4), (7, 7)])
    def test_sum_preserved(self, lots, expect_sum):
        a, b, c = eg.split_lots(lots)
        assert a + b + c == expect_sum

    def test_single_lot_degraded(self):
        assert eg.split_lots(1) == (0, 0, 1)
        assert eg.split_lots(2) == (1, 0, 1)

    def test_zero(self):
        assert eg.split_lots(0) == (0, 0, 0)


class TestBuildPlan:
    def test_long_r_and_targets(self):
        p = eg.build_plan(account="A", real_symbol="MA701", direction="LONG",
                          lots_open=6, entry_px=3000, init_stop=2960, atr=20)
        assert p["r_unit"] == pytest.approx(40)
        assert p["tp1_px"] == pytest.approx(3040)
        assert p["tp2_px"] == pytest.approx(3080)
        assert p["tp1_lots"] + p["tp2_lots"] + p["rest_lots"] == 6

    def test_short_targets_below_entry(self):
        p = eg.build_plan(account="A", real_symbol="MA701", direction="SHORT",
                          lots_open=3, entry_px=3000, init_stop=3040)
        assert p["tp1_px"] == pytest.approx(2960)
        assert p["tp2_px"] == pytest.approx(2920)

    def test_missing_stop_raises(self):
        with pytest.raises(ValueError):
            eg.build_plan(account="A", real_symbol="X", direction="LONG",
                          lots_open=1, entry_px=3000, init_stop=3000)

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError):
            eg.build_plan(account="A", real_symbol="X", direction="FLAT",
                          lots_open=1, entry_px=3000, init_stop=2900)


class TestEvaluate:
    def _plan(self, direction="LONG"):
        return eg.build_plan(account="A", real_symbol="MA701", direction=direction,
                             lots_open=6, entry_px=3000,
                             init_stop=2960 if direction == "LONG" else 3040, atr=20)

    def test_hard_stop_short_circuits(self):
        sigs = eg.evaluate(self._plan(), last_price=2950, mult=10)
        assert [s.kind for s in sigs] == ["HARD_STOP"]
        assert sigs[0].lots == 6

    def test_tp1_then_tp2(self):
        p = self._plan()
        assert [s.kind for s in eg.evaluate(p, last_price=3050, mult=10)] == ["TP1"]
        assert [s.kind for s in eg.evaluate(p, last_price=3090, mult=10)] == ["TP1", "TP2"]

    def test_time_stop_fires_when_no_progress(self):
        p = self._plan()
        now = datetime.now(timezone.utc)
        p["opened_at"] = now - timedelta(days=6)
        p["time_stop_at"] = now - timedelta(days=1)
        sigs = eg.evaluate(p, last_price=3010, mult=10, now=now)
        assert any(s.kind == "TIME_STOP" for s in sigs)

    def test_time_stop_not_fired_after_tp1(self):
        p = self._plan()
        now = datetime.now(timezone.utc)
        p["time_stop_at"] = now - timedelta(days=1)
        p["tp1_done_at"] = now - timedelta(days=2)
        sigs = eg.evaluate(p, last_price=3010, mult=10, now=now)
        assert not any(s.kind == "TIME_STOP" for s in sigs)


class TestTrail:
    def test_only_moves_favourably(self):
        p = eg.build_plan(account="A", real_symbol="X", direction="LONG",
                          lots_open=3, entry_px=3000, init_stop=2960, atr=20)
        p["tp2_done_at"] = "done"
        up = eg.update_trail(p, last_price=3100, atr_now=20)
        assert up == pytest.approx(3040)
        down = eg.update_trail({**p, "trail_stop": up}, last_price=3050, atr_now=20)
        assert down is None, "止损只允许朝有利方向移动"


class TestSizing:
    def test_margin_binds_when_risk_allows_more(self):
        """风险预算允许 9 手、保证金只允许 6 手 → 取 6 手，约束边是 margin。"""
        r = eg.size_with_double_constraint(
            equity=45000, per_lot_risk=50, per_lot_margin=3000,
            target_risk_pct=0.01, max_usage_pct=0.40, current_margin=0)
        assert r["by_risk"] == 9 and r["by_margin"] == 6
        assert r["binding"] == "margin"
        assert r["lots"] == 6

    def test_risk_binds_when_margin_ample(self):
        r = eg.size_with_double_constraint(
            equity=45000, per_lot_risk=900, per_lot_margin=500,
            target_risk_pct=0.01, max_usage_pct=0.40, current_margin=0)
        assert r["binding"] == "risk"

    def test_no_headroom_gives_zero(self):
        r = eg.size_with_double_constraint(
            equity=45000, per_lot_risk=100, per_lot_margin=3000,
            target_risk_pct=0.01, max_usage_pct=0.40, current_margin=18000)
        assert r["lots"] == 0

    def test_missing_input_refuses(self):
        r = eg.size_with_double_constraint(equity=45000, per_lot_risk=0,
                                           per_lot_margin=3000)
        assert r["lots"] == 0 and r["binding"] == "invalid"


class TestEntryAllowed:
    def test_red_blocks(self):
        ok, why = eg.entry_allowed("RED", {"allow_open": True, "reasons": []})
        assert not ok and "RED" in why[0]

    def test_unknown_blocks(self):
        ok, _ = eg.entry_allowed("UNKNOWN", {"allow_open": True, "reasons": []})
        assert not ok, "UNKNOWN 必须视为禁止开新仓"

    def test_yellow_blocks(self):
        ok, _ = eg.entry_allowed("YELLOW", {"allow_open": True, "reasons": []})
        assert not ok

    def test_green_and_daily_ok(self):
        ok, why = eg.entry_allowed("GREEN", {"allow_open": True, "reasons": []})
        assert ok and not why

    def test_daily_guard_blocks(self):
        ok, why = eg.entry_allowed(
            "GREEN", {"allow_open": False, "reasons": ["当日已连亏 2 笔"]})
        assert not ok and why


# ---------------------------------------------------------------------------
# 品种减法 / 接受标准
# ---------------------------------------------------------------------------
class TestAcceptance:
    def test_tail_dependency_fails(self):
        """剔除最大 3 笔后为负 → FAIL（归档教训 #16: 增量 108% 来自单一品种）。"""
        trades = [("SC", 900), ("SC", 800), ("SC", 700)] + [("MA", -100)] * 40
        r = vg.acceptance_check(trades, min_trades=10)
        assert r["verdict"] == "FAIL"
        assert "剔除最大 3 笔" in r["reason"]

    def test_fixed_symbol_set_can_flip_verdict(self):
        """全量赚、固定选品集亏 → 必须 FAIL（归档教训 #17）。"""
        trades = [("SC", 5000)] + [("MA", -100)] * 40
        r = vg.acceptance_check(trades, fixed_symbols=["MA"], min_trades=10)
        assert r["verdict"] == "FAIL"
        assert r["net_fixed"] is not None and r["net_fixed"] < 0

    def test_healthy_passes(self):
        trades = [(s, 100) for s in ("AG", "AU", "JM")] * 15
        r = vg.acceptance_check(trades, min_trades=30)
        assert r["verdict"] == "PASS", r["reason"]

    def test_small_sample_fails(self):
        r = vg.acceptance_check([("AG", 100)] * 3, min_trades=30)
        assert r["verdict"] == "FAIL"

    def test_empty(self):
        assert vg.acceptance_check([])["verdict"] == "NO_DATA"

    # ---- 回归：真实合约码 vs 品种码（2026-10-27 端到端验证时发现的静默失效）----
    def test_real_contract_symbols_match_product_whitelist(self):
        """``AG2612`` 必须能匹配白名单 ``AG``，否则 net_fixed 恒为 0。

        这是真实踩到的坑：``trade_journal.real_symbol`` 存 ``AG2612``，而
        ``VALIDATED_DAILY`` 存 ``AG``。原实现直接 ``str()`` 比较 →
        ``net_fixed`` 恒 0 → 恒判 FAIL，理由还写着"固定选品集净利 0 ≤ 0"。
        """
        trades = [("AG2612", 300)] * 10 + [("MA2701", -100)] * 30
        r = vg.acceptance_check(trades, fixed_symbols=["AG"], min_trades=10)
        assert r["net_fixed"] == 3000.0, r
        assert set(r["by_symbol"]) == {"AG", "MA"}, "品种维度必须归一为品种码"

    def test_accepts_trade_row_objects(self):
        """传 ``TradeRow`` 序列也必须能用（取 real_symbol；net = gross − fee）。"""
        from app.risk.trade_journal import TradeRow

        def row(sym, gross):
            return TradeRow(
                account="T", real_symbol=sym, direction="LONG",
                open_at=datetime(2026, 10, 1, 9, 0), close_at=datetime(2026, 10, 1, 10, 0),
                lots=1, open_px=1000.0, close_px=1000.0 + gross,
                gross=gross, fee=0.0, close_reason="TEST", source="TEST",
            )

        rows = [row("SC2612", 500)] * 5 + [row("MA2701", -100)] * 30
        r = vg.acceptance_check(rows, fixed_symbols=["SC"], min_trades=10)
        assert r["n"] == 35
        assert r["net_fixed"] == 2500.0
        assert r["net_all"] == 2500.0 - 3000.0

    def test_bad_element_raises(self):
        with pytest.raises(TypeError):
            vg.acceptance_check([("AG", 1, 2)])
        with pytest.raises(TypeError):
            vg.acceptance_check([object()])

    def test_product_code_idempotent(self):
        for s, want in [("AG2612", "AG"), ("MA701", "MA"), ("ag2612", "AG"),
                        ("AG", "AG"), (" SR2701 ", "SR"), ("sc", "SC")]:
            assert vg.product_code(s) == want, s
            assert vg.product_code(vg.product_code(s)) == want


class TestWhitelist:
    def test_validated_sets_are_disjoint_or_documented(self):
        assert "SC" in vg.VALIDATED_60M
        assert "MA" not in vg.VALIDATED_DAILY, "MA 未经样本外验证，不得进白名单"

    def test_best_freq_only_for_minute_validated(self):
        for k in vg.BEST_FREQ:
            assert k in vg.VALIDATED_60M
