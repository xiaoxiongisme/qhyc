import React from "react";
import { fmtNum } from "../api.jsx";
import { useData, Loading, ErrorBox, ReadyBadge } from "../hooks.jsx";

export default function Quality() {
  const { data, err } = useData("/dashboard/quality");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;

  const { readiness, calendar_mode, ingest_progress, anomalies, anomaly_open } = data;

  return (
    <>
      <div className="panel">
        <div className="kpi">
          <div className="item">
            <b>
              {readiness.status}
              <ReadyBadge readiness={readiness} />
            </b>
            <span>数据就绪门控（R3①）</span>
          </div>
          <div className="item">
            <b>{calendar_mode.mode}</b>
            <span>交易日历模式（R2：official / inferred）</span>
          </div>
          <div className="item">
            <b>{ingest_progress.progress}</b>
            <span>回补进度（R3②）</span>
          </div>
          <div className="item">
            <b className={anomaly_open > 0 ? "err" : ""}>{anomaly_open}</b>
            <span>待裁决异常工单（⑰）</span>
          </div>
        </div>
      </div>

      <h2>回补进度明细</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>品种</th><th>交易所</th><th>已入库</th><th>行数</th><th>最新日期</th>
            </tr>
          </thead>
          <tbody>
            {ingest_progress.symbols.map((s) => (
              <tr key={s.symbol}>
                <td>{s.symbol}</td>
                <td className="muted">{s.exchange}</td>
                <td>
                  {s.ingested ? (
                    <span className="badge ok">是</span>
                  ) : (
                    <span className="badge warn">否</span>
                  )}
                </td>
                <td>{s.rows}</td>
                <td>{s.latest || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>异常工单（最近 50 条 · ⑰ tqsdk 二次比对）</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>#</th><th>品种</th><th>日期</th><th>字段</th>
              <th>akshare</th><th>tqsdk</th><th>偏差%</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            {anomalies.map((a) => (
              <tr key={a.id}>
                <td>{a.id}</td>
                <td>{a.symbol}</td>
                <td>{a.trade_date}</td>
                <td>{a.field}</td>
                <td>{fmtNum(a.akshare_val, 2)}</td>
                <td>{fmtNum(a.tqsdk_val, 2)}</td>
                <td className={a.diff > 0.5 ? "err" : ""}>{fmtNum(a.diff, 2)}</td>
                <td>
                  <span className={"badge " + (a.status === "open" ? "warn" : "ok")}>
                    {a.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="small" style={{ marginTop: 8 }}>
          裁决接口：POST /anomalies/&#123;id&#125;/resolve（仅 accept_tqsdk / false_positive，⑰）
        </div>
      </div>
    </>
  );
}
