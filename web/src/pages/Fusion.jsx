import React, { useState, useEffect, useCallback } from "react";
import { getJSON } from "../api.jsx";
import { Loading, ErrorBox } from "../hooks.jsx";

function fmtPx(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
function fmtTime(v) {
  if (!v) return "—";
  return new Date(v).toLocaleString("zh-CN", { hour12: false });
}
const KIND_LABEL = {
  signal: "🚨 信号",
  presession: "🌅 盘前",
  heartbeat: "📊 简报",
  startup: "🟢 启动",
};
function posClass(p) {
  if (p === "LONG") return "dir-up";
  if (p === "SHORT") return "dir-down";
  return "muted";
}
function posText(p) {
  return p === "LONG" ? "🔴 多" : p === "SHORT" ? "🟢 空" : "— 空仓";
}

export default function Fusion() {
  const [pos, setPos] = useState(null);
  const [log, setLog] = useState(null);
  const [err, setErr] = useState(null);
  const [auto, setAuto] = useState(true);
  const [expanded, setExpanded] = useState({});

  const load = useCallback(async () => {
    try {
      const [p, l] = await Promise.all([
        getJSON("/fusion/positions"),
        getJSON("/fusion/push_log?limit=40"),
      ]);
      setPos(p);
      setLog(l);
      setErr(null);
    } catch (e) {
      setErr(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    if (!auto) return;
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [auto, load]);

  if (err) return <ErrorBox msg={err} />;
  if (!pos || !log) return <Loading />;

  const open = pos.rows.filter((r) => r.position !== "FLAT");

  return (
    <>
      <div className="panel">
        <div className="kpi">
          <div className="item">
            <b>{pos.count}</b>
            <span>监控品种</span>
          </div>
          <div className="item">
            <b className="dir-up">{open.filter((r) => r.position === "LONG").length}</b>
            <span>持多</span>
          </div>
          <div className="item">
            <b className="dir-down">{open.filter((r) => r.position === "SHORT").length}</b>
            <span>持空</span>
          </div>
          <div className="item">
            <b>{log.count}</b>
            <span>推送留痕</span>
          </div>
        </div>
        <div className="small">
          融合策略每 15 分钟扫描 · 信号/持仓/止损经 pushplus 推送至微信 ·
          本页每 60 秒自动刷新
          <button
            className={auto ? "active" : ""}
            style={{ marginLeft: 8 }}
            onClick={() => setAuto((a) => !a)}
          >
            {auto ? "自动刷新中" : "已暂停"}
          </button>
          <button style={{ marginLeft: 8 }} onClick={load}>手动刷新</button>
        </div>
      </div>

      <h2>当前持仓快照</h2>
      {open.length === 0 ? (
        <div className="panel muted">当前无持仓（全部 FLAT）。</div>
      ) : (
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>品种</th>
                <th>方向</th>
                <th>入场价</th>
                <th>入场时间</th>
                <th>上次推止损</th>
                <th>保本</th>
                <th>开仓信号</th>
              </tr>
            </thead>
            <tbody>
              {open.map((r) => (
                <tr key={r.symbol}>
                  <td className="sym">{r.symbol}</td>
                  <td className={posClass(r.position)}>{posText(r.position)}</td>
                  <td>{fmtPx(r.entry_price)}</td>
                  <td className="small">{fmtTime(r.entry_at)}</td>
                  <td>{fmtPx(r.last_pushed_stop)}</td>
                  <td>{r.be_done ? "🎯 已触发" : "—"}</td>
                  <td className="small">{fmtTime(r.signal_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>推送历史（最近 {log.count} 条）</h2>
      <div className="grid">
        {log.rows.map((row) => (
          <div className="card" key={row.id}>
            <div className="sym" style={{ display: "flex", justifyContent: "space-between" }}>
              <span>
                <span className="badge ok">{KIND_LABEL[row.kind] || row.kind}</span>{" "}
                {row.title}
              </span>
              <span className="small muted">
                {fmtTime(row.pushed_at)}{" "}
                {row.delivered ? (
                  <span className="badge ok">已送达·{row.via}</span>
                ) : (
                  <span className="badge warn">未送达</span>
                )}
              </span>
            </div>
            <div className="small muted" style={{ margin: "4px 0" }}>
              信号 {row.n_signals ?? 0} · 持仓 {row.n_rows ?? 0}
            </div>
            <button
              className="small"
              onClick={() => setExpanded((e) => ({ ...e, [row.id]: !e[row.id] }))}
            >
              {expanded[row.id] ? "收起正文" : "展开正文"}
            </button>
            {expanded[row.id] && (
              <pre className="pushbody">{row.content}</pre>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
