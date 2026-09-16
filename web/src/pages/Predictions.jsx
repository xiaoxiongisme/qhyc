import React, { useState } from "react";
import { fmtNum, fmtPct, dirClass, dirText } from "../api.jsx";
import { useData, Loading, ErrorBox } from "../hooks.jsx";

// §16.6 传导来源卡片：上游品种滞后涨跌 → 预期影响
function DriversCard({ drivers }) {
  if (!drivers || Object.keys(drivers).length === 0) {
    return (
      <div className="card" style={{ marginTop: 10 }}>
        <h3>传导来源</h3>
        <div className="small muted">暂无传导特征（新品种或特征构建失败）</div>
      </div>
    );
  }
  const rows = [
    { k: "板块动量 1d/5d/20d", v: `${fmtPct(drivers.sector_ret_1d)} / ${fmtPct(drivers.sector_ret_5d)} / ${fmtPct(drivers.sector_ret_20d)}` },
    { k: "品种−板块背离", v: fmtPct(drivers.sector_excess) },
    { k: "上游滞后涨幅 1d/5d", v: `${fmtPct(drivers.upstream_ret_lag1)} / ${fmtPct(drivers.upstream_ret_lag5)}` },
    { k: "成本传导缺口 cost_gap", v: fmtPct(drivers.cost_gap) },
    { k: "板块内相关度", v: fmtNum(drivers.sector_corr_regime, 2) },
    { k: "板块指数预测 P(up)", v: fmtNum(drivers.sector_pred_prob, 3) },
  ];
  return (
    <div className="card" style={{ marginTop: 10 }}>
      <h3>传导来源（§16.3/16.6）</h3>
      <table>
        <tbody>
          {rows.map((r) => (
            <tr key={r.k}>
              <td className="muted">{r.k}</td>
              <td>{r.v}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="small">
        全部为滞后数据（防前视）；te_topk 当前使用先验/TE 权重 top-3 上游方向
      </div>
    </div>
  );
}

export default function Predictions() {
  const [symbol, setSymbol] = useState("FG888");
  const [input, setInput] = useState("FG888");
  const { data: latest, err } = useData(
    `/predict?symbol=${symbol}&limit=10`
  );
  const { data: bars } = useData(`/bars/${symbol}?limit=30`);
  const { data: sectorData } = useData("/dashboard/sectors");

  if (err) return <ErrorBox msg={err} />;
  if (!latest) return <Loading />;

  const cur = latest[0];
  const sector = sectorData?.sectors?.find((s) =>
    s.members?.some((m) => m.symbol === symbol)
  );

  return (
    <>
      <div className="panel">
        <label>品种：</label>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          placeholder="如 FG888"
          style={{
            background: "var(--panel2)", color: "var(--text)",
            border: "1px solid var(--border)", borderRadius: 4, padding: "6px 10px",
          }}
        />{" "}
        <button
          onClick={() => setSymbol(input)}
          style={{
            background: "var(--accent)", color: "#fff", border: "none",
            borderRadius: 4, padding: "6px 16px", cursor: "pointer",
          }}
        >
          查询
        </button>
        {cur && (
          <span style={{ marginLeft: 16 }}>
            <span className={dirClass(cur.direction)} style={{ fontSize: 18 }}>
              {dirText(cur.direction)}
            </span>
            <span className="muted"> p={fmtNum(cur.direction_prob, 3)}</span>{" "}
            <span className="badge caliber">
              {(cur.caliber || "close") === "settle" ? "结算价口径" : "收盘价口径"}
            </span>
          </span>
        )}
      </div>

      {cur && (
        <div className="grid cols3">
          <div className="card">
            <h3>最新预测（§5.3 契约）</h3>
            <table>
              <tbody>
                <tr><td className="muted">run_id</td><td className="small">{cur.run_id}</td></tr>
                <tr><td className="muted">目标日</td><td>{cur.target_date}</td></tr>
                <tr><td className="muted">方向概率</td><td>{fmtNum(cur.direction_prob, 4)}</td></tr>
                <tr><td className="muted">幅度点估计</td><td>{fmtPct(cur.ret_point)}</td></tr>
                <tr><td className="muted">区间 [P5,P95]</td><td>[{fmtPct(cur.ret_low)}, {fmtPct(cur.ret_high)}]</td></tr>
                <tr><td className="muted">置信度</td><td>{fmtNum(cur.confidence, 2)}</td></tr>
                <tr><td className="muted">参与模型</td><td className="small">{(cur.participated_models || []).join(", ")}</td></tr>
              </tbody>
            </table>
          </div>
          <DriversCard drivers={cur.drivers} />
          <div className="card">
            <h3>板块背景（{sector?.sector || "—"}）</h3>
            {sector ? (
              <table>
                <tbody>
                  <tr><td className="muted">板块 1d/5d/20d</td><td>{fmtPct(sector.ret_1d)} / {fmtPct(sector.ret_5d)} / {fmtPct(sector.ret_20d)}</td></tr>
                  <tr><td className="muted">指数水平</td><td>{fmtNum(sector.index_level, 1)}（{sector.weight_method} 加权）</td></tr>
                  <tr><td className="muted">板块内排名</td><td>{sector.members.findIndex((m) => m.symbol === symbol) + 1} / {sector.members.length}</td></tr>
                </tbody>
              </table>
            ) : (
              <div className="small muted">未分类或无指数</div>
            )}
          </div>
        </div>
      )}

      <h2>近期日线（收盘价口径）</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th>
              <th>结算</th><th>量</th><th>ret_close</th><th>ret_settle</th>
            </tr>
          </thead>
          <tbody>
            {(bars || []).map((b) => (
              <tr key={b.trade_date}>
                <td>{b.trade_date}</td>
                <td>{fmtNum(b.open, 1)}</td>
                <td>{fmtNum(b.high, 1)}</td>
                <td>{fmtNum(b.low, 1)}</td>
                <td>{fmtNum(b.close, 1)}</td>
                <td>{fmtNum(b.settle, 1)}</td>
                <td>{b.volume}</td>
                <td className={b.ret_close > 0 ? "up" : "down"}>{fmtPct(b.ret_close)}</td>
                <td className="muted">{fmtPct(b.ret_settle)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>预测留痕（同 as_of 多 run 可追溯）</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>run_id</th><th>目标日</th><th>方向</th><th>p</th>
              <th>幅度</th><th>置信</th><th>状态</th><th>模型</th><th>口径</th>
            </tr>
          </thead>
          <tbody>
            {latest.map((p) => (
              <tr key={p.run_id}>
                <td className="small">{p.run_id}</td>
                <td>{p.target_date}</td>
                <td className={dirClass(p.direction)}>{dirText(p.direction)}</td>
                <td>{fmtNum(p.direction_prob, 3)}</td>
                <td>{fmtPct(p.ret_point)}</td>
                <td>{fmtNum(p.confidence, 2)}</td>
                <td>{p.state}</td>
                <td className="small">{(p.participated_models || []).length} 个</td>
                <td>{(p.caliber || "close") === "settle" ? "结算" : "收盘"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
