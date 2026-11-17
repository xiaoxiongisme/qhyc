import React, { useState } from "react";
import Overview from "./pages/Overview.jsx";
import Predictions from "./pages/Predictions.jsx";
import Sectors from "./pages/Sectors.jsx";
import Quality from "./pages/Quality.jsx";
import Volatility from "./pages/Volatility.jsx";
import Fusion from "./pages/Fusion.jsx";
import Anomalies from "./pages/Anomalies.jsx";
import Blindspot from "./pages/Blindspot.jsx";   // 少数面发现（L0 阅读线索；CL-20261116-01）
import V5xLiveBacktest from "./pages/V5xLiveBacktest.jsx";  // V5X live-like 回测（CL-20261117-03）

const TABS = [
  { key: "overview", label: "品种总览", comp: Overview },
  { key: "predictions", label: "预测详情", comp: Predictions },
  { key: "sectors", label: "大类热力图", comp: Sectors },
  { key: "volatility", label: "波动率", comp: Volatility },
  { key: "quality", label: "数据质量", comp: Quality },
  { key: "anomalies", label: "异常工单", comp: Anomalies },
  { key: "fusion", label: "融合信号", comp: Fusion },
  { key: "v5x_live_bt", label: "V5X实盘回测", comp: V5xLiveBacktest },
  { key: "blindspot", label: "少数面发现", comp: Blindspot },
];

export default function App() {
  const [tab, setTab] = useState("overview");
  const Active = TABS.find((t) => t.key === tab).comp;
  return (
    <>
      <h1>期货预测平台</h1>
      <div className="small">
        M5 看板 · 全部涨跌幅度为<span className="badge caliber">收盘价口径</span>（§17）
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? "active" : ""}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <Active />
    </>
  );
}
