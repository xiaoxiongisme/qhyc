import React, { useState } from "react";
import { fmtNum, fmtPct, retClass, postJSON } from "../api.jsx";
import { Loading, ErrorBox } from "../hooks.jsx";

// 融合策略两层回测（报告 G1 / PRD §8）：信号层 walk-forward + 组合层 FIFO 重放
// 信号与实盘引擎 fusion_state_detail 同一套循环（单一真源），无口径分叉。
export default function FusionBacktest() {
  const [form, setForm] = useState({
    symbols: "FG888,RB888,CU888",
    start: "2025-01-01",
    end: "2026-09-01",
    src: "akshare",
    sl_atr: 2.0,
    trail_atr: 2.0,
    cost_bp: 1.3,
    seeds: "",
  });
  const [res, setRes] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function runMatrix() {
    setLoading(true);
    setErr(null);
    try {
      const r = await postJSON("/backtest/fusion/matrix", {
        symbols: form.symbols ? form.symbols.split(",").map((s) => s.trim()) : null,
        start: form.start || null,
        end: form.end || null,
      });
      setMsg("矩阵已提交后台执行（见服务端日志 / task_run）");
      setRes(null);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function runBacktest() {
    setLoading(true);
    setErr(null);
    setMsg(null);
    try {
      const payload = {
        symbols: form.symbols ? form.symbols.split(",").map((s) => s.trim()) : null,
        start: form.start || null,
        end: form.end || null,
        src: form.src,
        sl_atr: Number(form.sl_atr),
        trail_atr: Number(form.trail_atr),
        cost_bp: Number(form.cost_bp),
        seeds: form.seeds ? form.seeds.split(",").map((s) => parseInt(s.trim(), 10)) : null,
        async_run: false,
      };
      const r = await postJSON("/backtest/fusion", payload);
      setRes(r);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="panel">
        <div className="grid2">
          <label>品种（逗号分隔，空=全部主连）
            <input value={form.symbols} onChange={set("symbols")} />
          </label>
          <label>起始日
            <input value={form.start} onChange={set("start")} placeholder="YYYY-MM-DD" />
          </label>
          <label>结束日
            <input value={form.end} onChange={set("end")} placeholder="YYYY-MM-DD" />
          </label>
          <label>小时线口径
            <select value={form.src} onChange={set("src")}>
              <option value="akshare">akshare</option>
              <option value="tqsdk">tqsdk</option>
            </select>
          </label>
          <label>初始止损 sl_atr×ATR
            <input type="number" step="0.1" value={form.sl_atr} onChange={set("sl_atr")} />
          </label>
          <label>吊灯止损 trail_atr×ATR
            <input type="number" step="0.1" value={form.trail_atr} onChange={set("trail_atr")} />
          </label>
          <label>双边成本（基点）
            <input type="number" step="0.1" value={form.cost_bp} onChange={set("cost_bp")} />
          </label>
          <label>多 seed（逗号分隔，可选）
            <input value={form.seeds} onChange={set("seeds")} placeholder="如 1,2,3" />
          </label>
        </div>
        <div style={{ marginTop: 10 }}>
          <button onClick={runBacktest} disabled={loading}>运行融合回测</button>
          <button onClick={runMatrix} disabled={loading} style={{ marginLeft: 8 }}>运行四维矩阵（后台）</button>
        </div>
        {msg && <div className="small muted" style={{ marginTop: 8 }}>{msg}</div>}
      </div>

      {loading && <Loading />}
      {err && <ErrorBox msg={err} />}

      {res && !res.error && (
        <>
          <h2>组合层指标</h2>
          <div className="panel">
            <div className="kpi">
              <div className="item"><b>{res.n_trades}</b><span>成交笔数</span></div>
              <div className="item"><b>{res.win_rate != null ? fmtPct(res.win_rate * 100, 1) : "—"}</b><span>胜率</span></div>
              <div className="item"><b className={retClass(res.total_return_pct)}>{res.total_return_pct != null ? fmtPct(res.total_return_pct, 2) : "—"}</b><span>总收益%</span></div>
              <div className="item"><b className={retClass(-(res.max_drawdown_pct || 0))}>{res.max_drawdown_pct != null ? fmtPct(-res.max_drawdown_pct, 2) : "—"}</b><span>最大回撤%</span></div>
              <div className="item"><b>{res.sharpe != null ? fmtNum(res.sharpe, 2) : "—"}</b><span>夏普(年化≈)</span></div>
            </div>
            {res.equity_curve && res.equity_curve.length > 1 && (
              <EquitySpark data={res.equity_curve} />
            )}
            <div className="small muted" style={{ marginTop: 8 }}>
              口径：单源 {res.params && res.params.src} 小时线 · close-only · 扣双边成本 {res.params && res.params.cost_bp}bp ·
              信号层与实盘 fusion_state_detail 同循环（单一真源）
            </div>
          </div>

          <h2>逐品种明细</h2>
          <div className="panel">
            <table>
              <thead>
                <tr>
                  <th>品种</th><th>笔数</th><th>胜率</th><th>总收益%</th><th>最大回撤%</th><th>夏普</th>
                </tr>
              </thead>
              <tbody>
                {(res.per_symbol || []).map((m) => (
                  <tr key={m.symbol}>
                    <td>{m.symbol}</td>
                    <td>{m.n_trades}</td>
                    <td>{m.win_rate != null ? fmtPct(m.win_rate * 100, 1) : "—"}</td>
                    <td className={retClass(m.total_return_pct)}>{m.total_return_pct != null ? fmtPct(m.total_return_pct, 2) : "—"}</td>
                    <td className={retClass(-(m.max_drawdown_pct || 0))}>{m.max_drawdown_pct != null ? fmtPct(-m.max_drawdown_pct, 2) : "—"}</td>
                    <td>{m.sharpe != null ? fmtNum(m.sharpe, 2) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {res.skipped && res.skipped.length > 0 && (
              <div className="small muted">跳过（数据不足）: {res.skipped.join(", ")}</div>
            )}
          </div>
        </>
      )}

      {res && res.error && <ErrorBox msg={res.error} />}
    </>
  );
}

function EquitySpark({ data }) {
  const w = 600, h = 120;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / span) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ marginTop: 8, background: "#0c1220", borderRadius: 6 }}>
      <polyline points={pts} fill="none" stroke="#4fd1c5" strokeWidth="1.5" />
    </svg>
  );
}
