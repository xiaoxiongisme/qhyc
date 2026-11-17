import React, { useState, useEffect } from "react";
import { fmtNum, getJSON, postJSON } from "../api.jsx";
import { Loading, ErrorBox } from "../hooks.jsx";

// V5X live-like 回测（CL-20261117-03）：与生产唯一推送源 V5X 共用引擎与口径。
// 口径由服务端 live.py 常量决定，前端不提供覆盖项——防止拿另一套策略的数字对比实盘。
export default function V5xLiveBacktest() {
  const [profile, setProfile] = useState(null);
  const [form, setForm] = useState({
    symbols: "",
    start: "2025-01-01",
    end: "",
  });
  const [res, setRes] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    // 从守卫只读端点取当前口径：用一个最小请求让服务端回显 profile。
    // 直接展示后端常量（经 /health 无关的轻量探测），失败不影响主流程。
    getJSON("/backtest/v5x/profile").then((d) => setProfile(d.profile)).catch(() => {});
  }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function run() {
    setLoading(true);
    setErr(null);
    setRes(null);
    try {
      const r = await postJSON("/backtest/v5x/live-like", {
        symbols: form.symbols ? form.symbols.split(",").map((s) => s.trim()) : null,
        start: form.start || null,
        end: form.end || null,
        async_run: false,
      });
      if (r.error) setErr(r.error);
      else setRes(r);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  const pf = profile || res?.profile;
  return (
    <>
      <div className="panel">
        <div className="small" style={{ marginBottom: 8 }}>
          <span className="badge caliber">V5X.3 · live-like</span>{" "}
          与生产唯一推送源 V5X <b>同源</b>：参数取 live.py 常量，下一根开盘成交（E1-C）。
          与「回测面板」（预测模型）和「融合回测」（已停用的 V3.4）<b>不是同一策略</b>。
        </div>
        {pf && (
          <div className="small">
            当前口径：src={pf.src} · gate_lag={pf.gate_lag} · regime_mode={pf.regime_mode} ·
            fill_mode={pf.fill_mode} · 滑点 {pf.fill_slip_ticks}tick · 跳空 {pf.max_adverse_gap_atr}ATR
          </div>
        )}
        <div className="grid2">
          <label>品种（逗号分隔，空=38 白名单全部）
            <input value={form.symbols} onChange={set("symbols")} placeholder="RB888,CU888,FG888" />
          </label>
          <label>起始日
            <input value={form.start} onChange={set("start")} placeholder="2015-01-01" />
          </label>
          <label>结束日（空=今日）
            <input value={form.end} onChange={set("end")} placeholder="YYYY-MM-DD" />
          </label>
        </div>
        <button onClick={run} disabled={loading}>{loading ? "运行中…" : "运行 live-like 回测"}</button>
        <div className="small" style={{ marginTop: 6 }}>
          全池 12 年约 2 分钟；口径不可在此页修改（由服务端强制）。
        </div>
      </div>
      {err && <ErrorBox msg={err} />}
      {loading && <Loading />}
      {res && !res.error && (
        <div className="panel">
          <div className="grid2">
            <div>成交笔数 <b>{res.n_trades?.toLocaleString()}</b></div>
            <div>净利 <b className={res.net_yuan >= 0 ? "up" : "down"}>¥{fmtNum(res.net_yuan, 0)}</b></div>
            <div>胜率 <b>{fmtPctSafe(res.win_rate)}</b></div>
            <div>盈亏比 <b>{fmtNum(res.profit_factor, 3)}</b></div>
            <div>平均 R <b>{fmtNum(res.avg_r, 3)}</b></div>
            <div>成交口径 <b>{res.fill_mode}</b></div>
          </div>
          {res.profile && (
            <div className="small" style={{ marginTop: 8 }}>
              本报告口径：gate_lag={res.profile.gate_lag} · regime_mode={res.profile.regime_mode} ·
              src={res.profile.src}（与实盘一致）
            </div>
          )}
          <div className="small" style={{ marginTop: 8, color: "#888" }}>
            {res.disclaimer}
          </div>
        </div>
      )}
    </>
  );
}

function fmtPctSafe(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(Number(v) * 100).toFixed(2)}%`;
}
