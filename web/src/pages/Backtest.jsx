import React, { useState } from "react";
import { fmtNum, fmtPct } from "../api.jsx";
import { useData, Loading, ErrorBox } from "../hooks.jsx";

// 复验建议：ensemble 措辞 —— "未显著优于随机"而非"准确率 51%"
function significance(acc, wilsonLo, wilsonHi) {
  if (acc === null || wilsonLo === undefined) return null;
  if (wilsonLo > 0.5) return { cls: "up", text: "方向准确率显著高于随机" };
  if (wilsonHi < 0.5) return { cls: "down", text: "显著低于随机（存在微弱反向信息）" };
  return { cls: "muted", text: "未显著优于随机（与抛硬币无异）" };
}

export default function Backtest() {
  const { data, err } = useData("/dashboard/backtest-summary");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;
  if (!data.run_id) return <div className="panel muted">暂无整批回测 run</div>;

  const ensemble = data.models.find((m) => m.model === "ensemble");
  const ensSig = ensemble && significance(ensemble.dir_acc, ensemble.wilson_lo, ensemble.wilson_hi);

  return (
    <>
      <div className="panel">
        <div className="kpi">
          <div className="item">
            <b>{data.run_id}</b>
            <span>最新整批回测 run</span>
          </div>
          <div className="item">
            <b>{data.models.filter((m) => m.model !== "ensemble").length}</b>
            <span>评估模型</span>
          </div>
          <div className="item">
            <b>{ensemble ? ensemble.sample_n : "—"}</b>
            <span>ensemble 评估点数</span>
          </div>
        </div>
        <div className="small">
          口径：{data.caliber === "settle" ? "结算价" : "收盘价"} ｜ {data.note}
        </div>
        {ensSig && (
          <div style={{ marginTop: 8 }}>
            <b>融合策略（ensemble）：</b>
            <span className={ensSig.cls}>{ensSig.text}</span>
            <span className="muted">
              {" "}
              — dir_acc={fmtPct(ensemble.dir_acc * 100, 1)}
              （Wilson 95% CI [{fmtPct(ensemble.wilson_lo * 100, 1)}, {fmtPct(ensemble.wilson_hi * 100, 1)}]，
              n={ensemble.sample_n}）
            </span>
          </div>
        )}
      </div>

      <h2>模型指标（§18.3：主指标 = 加权准确率 + IC + 覆盖率；按 dir_acc_weighted 排序）</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>模型</th>
              <th>加权准确率↑</th>
              <th>dir_acc</th>
              <th>IC / rank_IC</th>
              <th>ICIR</th>
              <th>覆盖率</th>
              <th>净P&L/笔</th>
              <th>Wilson 95% CI</th>
              <th>n</th>
              <th>区间校准</th>
            </tr>
          </thead>
          <tbody>
            {data.models.map((m) => {
              const sig = significance(m.dir_acc, m.wilson_lo, m.wilson_hi);
              return (
                <tr key={m.model}>
                  <td>
                    {m.model}
                    {m.model === "ensemble" && <span className="badge ok">基准</span>}
                    {m.model === "reversal" && <span className="badge caliber">门控</span>}
                  </td>
                  <td className={sig ? sig.cls : ""}>
                    {m.dir_acc_weighted !== undefined && m.dir_acc_weighted !== null
                      ? fmtPct(m.dir_acc_weighted * 100, 1)
                      : "—"}
                  </td>
                  <td className="muted">{fmtPct((m.dir_acc || 0) * 100, 1)}</td>
                  <td>
                    {m.ic != null ? fmtNum(m.ic, 3) : "—"}
                    <span className="muted"> / {m.rank_ic != null ? fmtNum(m.rank_ic, 3) : "—"}</span>
                  </td>
                  <td>{m.icir != null ? fmtNum(m.icir, 3) : "—"}</td>
                  <td className="muted">
                    {m.coverage != null ? fmtPct(m.coverage * 100, 0) : "100%"}
                  </td>
                  <td className={(m.net_pnl_mean || 0) > 0 ? "up" : "down"}>
                    {m.net_pnl_mean != null ? `${m.net_pnl_mean > 0 ? "+" : ""}${fmtNum(m.net_pnl_mean, 4)}%` : "—"}
                  </td>
                  <td className="small">
                    [{fmtPct((m.wilson_lo || 0) * 100, 1)}, {fmtPct((m.wilson_hi || 0) * 100, 1)}]
                  </td>
                  <td className="muted">
                    {m.sample_n}
                    {m.coverage != null && m.coverage < 1 && (
                      <span className="small muted"> (sig {Math.round(m.sample_n * m.coverage)})</span>
                    )}
                  </td>
                  <td>
                    {m.interval_calibrated ? (
                      <span className="badge ok">已校准</span>
                    ) : (
                      <span className="badge warn">未校准/区间待重跑</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="small" style={{ marginTop: 8 }}>
          加权准确率 = 按 |实际涨跌| 加权（§18.3，大波动权重高，主指标）；
          IC = 预测幅度与实际收益相关系数；净 P&L/笔 = 方向策略每笔净收益（扣双边成本 0.065%）；
          覆盖率 = 发信号样本占比（reversal 门控内"无观点"不计入命中分母）；
          rf/xgb/wavelet 区间未校准；全部为收盘价口径
        </div>
      </div>

      <h2>覆盖率-准确率曲线（按 |预测幅度| 取 top x% 样本 · §18.3）</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>模型</th>
              {data.models
                .filter((m) => m.coverage_curve && m.coverage_curve.length)
                .slice(0, 1)
                .flatMap((m) => m.coverage_curve.map((c) => (
                  <th key={c.top_frac}>top {Math.round(c.top_frac * 100)}%</th>
                )))}
            </tr>
          </thead>
          <tbody>
            {data.models
              .filter((m) => m.coverage_curve && m.coverage_curve.length)
              .map((m) => (
                <tr key={m.model}>
                  <td>{m.model}</td>
                  {m.coverage_curve.map((c) => (
                    <td key={c.top_frac}>
                      {c.dir_acc != null ? (
                        <span className={c.dir_acc > 0.53 ? "up" : c.dir_acc < 0.5 ? "down" : ""}>
                          {fmtPct(c.dir_acc * 100, 1)}
                        </span>
                      ) : "—"}
                      <span className="small muted"> n={c.n}</span>
                    </td>
                  ))}
                </tr>
              ))}
          </tbody>
        </table>
        <div className="small" style={{ marginTop: 8 }}>
          理想信号：样本越收窄（置信度越高）准确率越高。reversal 期望在 top 10–30%
          处显著高于其全覆盖水平（§18.12：阈值年度重估，禁止硬编码历史最优值）
        </div>
      </div>

      <h2>Hurst 状态分层（§6）</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>模型</th>
              <th>trend</th>
              <th>neutral</th>
              <th>mean_revert</th>
            </tr>
          </thead>
          <tbody>
            {data.models
              .filter((m) => m.by_state && Object.keys(m.by_state).length)
              .map((m) => (
                <tr key={m.model}>
                  <td>{m.model}</td>
                  {["trend", "neutral", "mean_revert"].map((st) => {
                    const v = m.by_state[st];
                    return (
                      <td key={st}>
                        {v ? `${fmtPct(v.dir_acc * 100, 1)} (n=${v.n})` : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
