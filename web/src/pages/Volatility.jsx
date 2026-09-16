import React from "react";
import { fmtNum, fmtPct } from "../api.jsx";
import { useData, Loading, ErrorBox } from "../hooks.jsx";

// §18.7 M6c：波动率预测准度 + 异常波动预警
export default function Volatility() {
  const { data, err } = useData("/dashboard/volatility");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;

  const { latest, history } = data;
  const alerts = latest.filter((r) => r.high_vol_alert);

  return (
    <>
      <div className="panel">
        <div className="kpi">
          <div className="item">
            <b>{latest.length}</b>
            <span>品种有波动率预测（GARCH）</span>
          </div>
          <div className="item">
            <b className="err">{alerts.length}</b>
            <span>异常波动预警（截面 top 20%）</span>
          </div>
          <div className="item">
            <b>{history.length}</b>
            <span>历史 vol_hit 样本品种</span>
          </div>
        </div>
        <div className="small">
          σ_t 为 GARCH(1,1) 次日条件波动率预测（%）；vol_low/high 为 |收益| 的
          P5–P95 区间（半正态分位，理论覆盖 90%）。异常波动预警=该品种 σ_t 在
          当日全市场截面 top 20%（§18.7：对止损位/仓位管理提供输入）
        </div>
      </div>

      {alerts.length > 0 && (
        <>
          <h2>⚠ 异常波动预警</h2>
          <div className="grid cols4">
            {alerts.map((r) => (
              <div className="card" key={r.symbol}>
                <div className="sym err">{r.symbol}</div>
                <div>
                  σ = <b>{fmtNum(r.vol_point, 2)}%</b>
                </div>
                <div className="small">
                  |ret| 区间 [{fmtNum(r.vol_low, 3)}, {fmtNum(r.vol_high, 2)}]%（90% 置信）
                </div>
                <div className="small muted">目标日 {r.target_date}</div>
              </div>
            ))}
          </div>
        </>
      )}

      <h2>全部品种波动率预测</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>品种</th><th>σ_t (%)</th><th>|ret| P5</th><th>|ret| P95</th><th>预警</th>
            </tr>
          </thead>
          <tbody>
            {latest.map((r) => (
              <tr key={r.symbol}>
                <td>{r.symbol}</td>
                <td><b>{fmtNum(r.vol_point, 2)}</b></td>
                <td className="muted">{fmtNum(r.vol_low, 3)}</td>
                <td>{fmtNum(r.vol_high, 2)}</td>
                <td>{r.high_vol_alert ? <span className="badge warn">高波动</span> : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {history.length > 0 && (
        <>
          <h2>波动率预测准度（回测 vol_hit，理论 0.9）</h2>
          <div className="panel">
            <table>
              <thead>
                <tr><th>品种</th><th>vol_hit</th><th>vol_rmse</th></tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.symbol}>
                    <td>{h.symbol}</td>
                    <td className={h.vol_hit >= 0.85 ? "up" : h.vol_hit < 0.8 ? "err" : ""}>
                      {fmtPct(h.vol_hit * 100, 1)}
                    </td>
                    <td className="muted">{fmtNum(h.vol_rmse, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="small" style={{ marginTop: 8 }}>
              vol_hit ≥ 85% 视为区间校准良好；&lt; 80% 说明 σ_t 系统性偏窄（需要
              vol_scale 校准，与 P1-3 区间校准同机制）
            </div>
          </div>
        </>
      )}
    </>
  );
}
