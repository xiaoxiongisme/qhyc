import React from "react";
import { fmtNum, fmtPct, retClass } from "../api.jsx";
import { useData, Loading, ErrorBox } from "../hooks.jsx";

function heatColor(v, maxAbs) {
  if (v === null || v === undefined || !maxAbs) return "#2a3552";
  const ratio = Math.max(-1, Math.min(1, v / maxAbs));
  // 红=涨 绿=跌（A 股习惯）
  return ratio >= 0
    ? `rgba(224, 82, 82, ${0.25 + 0.65 * ratio})`
    : `rgba(59, 167, 118, ${0.25 + 0.65 * Math.abs(ratio)})`;
}

export default function Sectors() {
  const { data, err } = useData("/dashboard/sectors");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;

  const sectors = data.sectors || [];
  const maxAbs = Math.max(
    ...sectors.flatMap((s) => [Math.abs(s.ret_1d || 0), Math.abs(s.ret_5d || 0)]),
    0.1
  );
  const maxAbsMember = Math.max(
    ...sectors.flatMap((s) => s.members.map((m) => Math.abs(m.ret_1d || 0))),
    0.1
  );

  return (
    <>
      <div className="panel small">
        板块指数 = 成员品种涨跌幅按成交量加权合成（成交额缺失自动回退，成分每日留痕）；
        全部为收盘价口径。
      </div>

      <h2>板块强弱排名（5 日动量）</h2>
      <div className="panel heat">
        {sectors.map((s) => (
          <div className="heat-row" key={s.sector}>
            <div className="heat-name">{s.sector}</div>
            <div
              className="heat-bar"
              style={{ width: `${Math.min(100, Math.abs(s.ret_5d || 0) / maxAbs * 100 * 3)}px`, background: heatColor(s.ret_5d, maxAbs) }}
              title={`5d=${fmtPct(s.ret_5d)}`}
            />
            <div>
              <span className={retClass(s.ret_1d)}>1d {fmtPct(s.ret_1d)}</span>
              <span className="muted"> ｜ 5d </span>
              <span className={retClass(s.ret_5d)}>{fmtPct(s.ret_5d)}</span>
              <span className="muted"> ｜ 20d </span>
              <span className={retClass(s.ret_20d)}>{fmtPct(s.ret_20d)}</span>
              <span className="muted small"> ｜ 指数 {fmtNum(s.index_level, 1)}</span>
            </div>
          </div>
        ))}
      </div>

      {sectors.map((s) => (
        <div key={s.sector}>
          <h2>
            {s.sector}{" "}
            <span className="muted small">
              （{s.trade_date} · 成员 {s.members.length} · 指数 {fmtNum(s.index_level, 1)}）
            </span>
          </h2>
          <div className="panel">
            <table>
              <thead>
                <tr>
                  <th>品种</th>
                  <th>当日涨跌（热力）</th>
                  <th>ret_close</th>
                  <th>ret_settle（参照）</th>
                </tr>
              </thead>
              <tbody>
                {s.members.map((m) => (
                  <tr key={m.symbol}>
                    <td>{m.product}</td>
                    <td>
                      <div
                        style={{
                          width: `${Math.min(100, Math.abs(m.ret_1d || 0) / maxAbsMember * 100 * 2)}px`,
                          height: 12,
                          borderRadius: 2,
                          display: "inline-block",
                          background: heatColor(m.ret_1d, maxAbsMember),
                        }}
                      />
                    </td>
                    <td className={retClass(m.ret_1d)}>{fmtPct(m.ret_1d)}</td>
                    <td className="muted">{fmtPct(m.ret_settle_1d)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </>
  );
}
