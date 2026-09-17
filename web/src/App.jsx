import React, { useState } from "react";
import Overview from "./pages/Overview.jsx";
import Predictions from "./pages/Predictions.jsx";
import Sectors from "./pages/Sectors.jsx";
import Backtest from "./pages/Backtest.jsx";
import Quality from "./pages/Quality.jsx";
import Volatility from "./pages/Volatility.jsx";
import Fusion from "./pages/Fusion.jsx";
import FusionBacktest from "./pages/FusionBacktest.jsx";

const TABS = [
  { key: "overview", label: "品种总览", comp: Overview },
  { key: "predictions", label: "预测详情", comp: Predictions },
  { key: "sectors", label: "大类热力图", comp: Sectors },
  { key: "volatility", label: "波动率", comp: Volatility },
  { key: "backtest", label: "回测面板", comp: Backtest },
  { key: "quality", label: "数据质量", comp: Quality },
  { key: "fusion", label: "融合信号", comp: Fusion },
  { key: "fusion_bt", label: "融合回测", comp: FusionBacktest },
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
