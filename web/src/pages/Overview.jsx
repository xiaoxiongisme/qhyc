import React from "react";
import { fmtNum, fmtPct, dirClass, dirText } from "../api.jsx";
import { useData, Loading, ErrorBox } from "../hooks.jsx";

export default function Overview() {
  const { data, err } = useData("/dashboard/overview");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;

  const { readiness, symbols } = data;
  const predicted = symbols.filter((s) => s.prediction);
  const up = predicted.filter((s) => s.prediction.direction === "up").length;

  return (
    <>
      <div className="panel">
        <div className="kpi">
          <div className="item">
            <b>{symbols.length}</b>
            <span>覆盖品种</span>
          </div>
          <div className="item">
            <b>{predicted.length}</b>
            <span>已有预测</span>
          </div>
          <div className="item">
            <b className="dir-up">{up}</b>
            <span>看涨 / 看跌 {predicted.length - up}</span>
          </div>
          <div className="item">
            <b>
              {readiness.ready ? "就绪" : "回补中"}
              {readiness.ready_ratio !== undefined
                ? ` ${Math.round(readiness.ready_ratio * 100)}%`
                : ""}
            </b>
            <span>数据就绪门控（R3①）</span>
          </div>
        </div>
        <div className="small">
          预测为下一交易日方向判断 · 全部幅度为收盘价口径（§17） ·
          完整契约见 /docs
        </div>
      </div>

      <h2>品种总览（按板块分组）</h2>
      <div className="grid cols3">
        {symbols.map((s) => (
          <div className="card" key={s.symbol}>
            <div className="sym">
              {s.symbol} <span className="muted">{s.name}</span>
            </div>
            <div className="small">
              {s.sector || "未分类"} · 数据至 {s.data_latest || "—"}（{s.rows} 根）
            </div>
            {s.prediction ? (
              <div style={{ marginTop: 6 }}>
                <span className={dirClass(s.prediction.direction)}>
                  {dirText(s.prediction.direction)}
                </span>
                <span className="muted">
                  {" "}
                  p={fmtNum(s.prediction.direction_prob, 3)} 幅度=
                  {fmtPct(s.prediction.ret_point)}
                </span>
                <div className="small">
                  目标 {s.prediction.target_date} · 置信度{" "}
                  {fmtNum(s.prediction.confidence, 2)} ·{" "}
                  {s.prediction.caliber === "settle" ? "结算价" : "收盘价"}口径
                </div>
              </div>
            ) : (
              <div className="small muted" style={{ marginTop: 6 }}>
                暂无预测记录
              </div>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
