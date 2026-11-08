"""V5X 推送通道：备用通道 + 失败补发 + 失败上抛（CL-20261109-06）。

V5X 在 2026-11-08 接管唯一推送源后，推送可靠性等价于策略可用性。而它从 V3.4
移植时漏抄了三处保护，这组测试逐个钉住：

  1. **备用通道**：`send_notify` 必须带上 `fusion.fallback_webhook`。
     V3.4 在 `scheduler.py` 传了，V5X 没传 —— pushplus 限流/故障时无降级路径。
  2. **失败补发**：`_persist` 用 `ON CONFLICT (dedupe_key) DO NOTHING`，推送失败时
     那一行**已经落库**，下一轮 `_persist` 返回空。若只推「本轮新增」，这条信号
     永远不会再发 —— 用户看到的是「这个小时没信号」。
  3. **补推标记**：补发的是旧 bar，价格可能已不可用，标题必须能区分，
     否则会被当成刚出的信号去下单。
  4. **失败不静默**：见 `tests/` 外的守卫 `V5X-PUSH-FAILURE-LOUD`（作业层外抛）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.strategies.v5x import live


def _sig(**kw):
    base = dict(
        symbol="RB888", action="OPEN", side="LONG",
        bar_ts="2026-11-06 14:15", adj_offset=411.0,
        price_ba=3430.0, price_real=3019.0, lots=1.0,
        stop_ba=3380.0, stop_real=2969.0, etype="bk",
    )
    base.update(kw)
    return live.V5XSignal(**base)


class _FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class _FakeFusion:
    fallback_webhook = "https://qyapi.example.invalid/hook?key=k"


class _FakeSettings:
    fusion = _FakeFusion()


@pytest.fixture
def wired(monkeypatch):
    """把 run_v5x_scan 的外部依赖全部替掉，返回可注入的状态字典。"""
    state: dict = {"sent": [], "marked": None, "queue": [], "new": [],
                   "ok": (True, "pushplus")}

    monkeypatch.setattr(live, "scan_once", lambda session, **kw: [])
    monkeypatch.setattr(live, "_persist", lambda session, sigs: state["new"])
    monkeypatch.setattr(live, "_load_unsent", lambda session, now, **kw: state["queue"])
    monkeypatch.setattr(
        live, "_mark_notify",
        lambda session, sigs, ok, via: state.update(marked=(len(sigs), ok, via)))
    monkeypatch.setattr("app.core.config.get_settings", lambda: _FakeSettings())

    def _send(title, content, fallback=""):
        state["sent"].append({"title": title, "content": content, "fallback": fallback})
        return state["ok"]

    monkeypatch.setattr("app.notify.send_notify", _send)
    return state


# ---------------------------------------------------------------- 备用通道
def test_push_passes_fallback_webhook(wired):
    """pushplus 挂了要有降级通道 —— V5X 移植时正是漏了这一项。"""
    wired["queue"] = [_sig()]
    live.run_v5x_scan(_FakeSession(), do_push=True)
    assert wired["sent"], "应当发生一次推送"
    assert wired["sent"][0]["fallback"] == _FakeFusion.fallback_webhook


# ---------------------------------------------------------------- 失败补发
def test_push_failure_is_marked_unsent(wired):
    """推送失败要留下 notify_ok=false，并如实上报 pushed=False。"""
    wired["queue"] = [_sig()]
    wired["ok"] = (False, "none")

    res = live.run_v5x_scan(_FakeSession(), do_push=True)

    assert res["pushed"] is False
    assert res["n_unsent"] == 1
    assert wired["marked"] == (1, False, "none"), "失败必须显式标记，不能只记日志"


def test_no_new_signals_still_pushes_backlog(wired):
    """本轮扫不到新信号（全被去重）但队列里有未送达 → 必须补发。

    这是最容易写错的地方：旧实现 `if not new: return`，一旦推送失败过一次，
    那条信号就永远发不出去。
    """
    wired["new"] = []
    wired["queue"] = [_sig(from_retry=True)]

    res = live.run_v5x_scan(_FakeSession(), do_push=True)

    assert res["pushed"] is True
    assert res["retry"] is True
    assert "补推" in wired["sent"][0]["title"]


def test_all_delivered_does_not_repush(wired):
    """队列空 → 不推送、不重复发。"""
    wired["queue"] = []

    res = live.run_v5x_scan(_FakeSession(), do_push=True)

    assert res["n_unsent"] == 0
    assert res["pushed"] is False
    assert wired["sent"] == []
    assert wired["marked"] is None


def test_fresh_signals_are_not_marked_retry(wired):
    """全是本轮新信号时不该打「补推」标记，否则用户以为出了故障。"""
    s = _sig()
    wired["new"] = [s]
    wired["queue"] = [s]

    res = live.run_v5x_scan(_FakeSession(), do_push=True)

    assert res["retry"] is False
    assert "补推" not in wired["sent"][0]["title"]


def test_do_push_false_skips_everything(wired):
    """回放/干跑模式：落库但不推送、不查队列。"""
    wired["new"] = [_sig()]
    wired["queue"] = [_sig()]

    res = live.run_v5x_scan(_FakeSession(), do_push=False)

    assert res["pushed"] is False and res["n_unsent"] == 0
    assert wired["sent"] == []


# ---------------------------------------------------------------- 队列读取
class _RowSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return self

    def mappings(self):
        return self

    def all(self):
        return self._rows


def test_load_unsent_maps_nulls():
    """DB 里的 NULL 要正确转成 None / 默认手数，别把 None 塞进格式化。"""
    rows = [{
        "dedupe_key": "AG888|2026-11-06 14:15:00+08:00|CLOSE|SHORT",
        "symbol": "AG888", "action": "CLOSE", "side": "SHORT",
        "bar_ts": datetime(2026, 11, 6, 14, 15), "adj_offset": 0.5,
        "price_ba": 100.0, "price_real": 99.5, "lots": None,
        "stop_ba": None, "stop_real": None,
        "etype": None, "reason": "stop", "net_yuan": None,
        "was_failed": True,
    }]
    got = live._load_unsent(_RowSession(rows), datetime(2026, 11, 9, 7, 0))

    assert len(got) == 1
    s = got[0]
    assert s.lots == 1.0                    # NULL 手数不能变成 0
    assert s.stop_ba is None and s.stop_real is None
    assert s.net_yuan is None
    assert s.etype == ""                    # NULL 文本给空串，文案里才不印 None
    assert s.source == "live"               # 补发队列只装实盘信号
    assert s.from_retry is True             # notify_ok=false → 是补发


def test_load_unsent_fresh_row_is_not_retry():
    """notify_ok 为 NULL（刚落库）的行不能标成补发，否则标题会误报故障。"""
    rows = [{
        "dedupe_key": "RB888|2026-11-06 14:15:00+08:00|OPEN|LONG",
        "symbol": "RB888", "action": "OPEN", "side": "LONG",
        "bar_ts": datetime(2026, 11, 6, 14, 15), "adj_offset": 0.0,
        "price_ba": 100.0, "price_real": 100.0, "lots": 1.0,
        "stop_ba": 90.0, "stop_real": 90.0,
        "etype": "bk", "reason": None, "net_yuan": None,
        "was_failed": False,
    }]
    got = live._load_unsent(_RowSession(rows), datetime(2026, 11, 9, 7, 0))
    assert got[0].from_retry is False


def test_stored_key_wins_over_recomputed():
    """从库读回的行必须用表里的 dedupe_key 回写，不能拿 bar_ts 重算。

    ★ 这是 2026-11-09 端到端验证抓到的真 bug：`dedupe_key` 里嵌着 `bar_ts`，
    落库时是 pandas `Timestamp`（str 可能带 `.000000`）、读回来是 `datetime`。
    两者格式化不保证一致 → `_mark_notify` 的 `WHERE dedupe_key=:k` 匹配 0 行
    → 信号永远标不成「已送达」→ 2 小时窗口内每轮都重推（连收 4 条重复消息）。
    """
    stored = "RB888|2026-11-06 14:15:00.000000+08:00|OPEN|LONG"
    rows = [{
        "dedupe_key": stored,
        "symbol": "RB888", "action": "OPEN", "side": "LONG",
        "bar_ts": datetime(2026, 11, 6, 14, 15),     # 无 tz、无微秒 → 重算必不同
        "adj_offset": 0.0, "price_ba": 100.0, "price_real": 100.0, "lots": 1.0,
        "stop_ba": None, "stop_real": None,
        "etype": "bk", "reason": None, "net_yuan": None,
        "was_failed": False,
    }]
    got = live._load_unsent(_RowSession(rows), datetime(2026, 11, 9, 7, 0))[0]

    assert got.dedupe_key == stored, "必须原样使用库里的 key"
    recomputed = f"{got.symbol}|{got.bar_ts}|{got.action}|{got.side}"
    assert got.dedupe_key != recomputed, "重算结果确实不同 —— 这正是必须存 key 的原因"


def test_retry_window_is_short():
    """补发窗口必须短：超过 2 小时的信号价格已不可用，补推等于误导下单。"""
    assert 0 < live.RETRY_WINDOW_HOURS <= 4
