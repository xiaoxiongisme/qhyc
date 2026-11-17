// 少数面发现（L0 阅读线索）—— blindspot schema 只读展示页
// CL-20261116-01：与交易链路硬隔离；这里的数据不可用于交易参考。
import React from "react";
import { useData, Loading, ErrorBox } from "../hooks.jsx";

const DISCLAIMER = "L0 阅读线索：可用于阅读、不可用于交易参考。本页非交易信号，不进入任何下单/预测/回测链路。";

function Banner() {
  return (
    <div className="panel" style={{ borderLeft: "4px solid #c0392b", background: "#fff6f5" }}>
      <b style={{ color: "#c0392b" }}>阅读线索，非交易信号</b>
      <div className="muted" style={{ marginTop: 4 }}>{DISCLAIMER}</div>
    </div>
  );
}

function MetaPanel() {
  const { data, err } = useData("/blindspot/meta");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;
  const c = data.counts || {};
  return (
    <div className="panel">
      <div className="kpi">
        <div className="item"><b>{c.items ?? "—"}</b><span>采集条目</span></div>
        <div className="item"><b>{c.signals ?? "—"}</b><span>少数面信号</span></div>
        <div className="item"><b>{c.quotes ?? "—"}</b><span>行情快照</span></div>
        <div className="item"><b>{c.pit_snapshots ?? "—"}</b><span>PIT 快照</span></div>
      </div>
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
        同步水位 asof：{data.meta?.last_sync_asof?.value || "—"} ｜ 来源：{data.meta?.source_db?.value || "—"}
      </div>
    </div>
  );
}

function SnapshotPanel() {
  const { data, err } = useData("/blindspot/snapshot");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;
  const s = data.snapshot;
  if (!s) return <div className="panel muted">暂无 PIT 快照</div>;
  return (
    <div className="panel">
      <b>PIT 快照 · {s.asof}</b>
      <div style={{ marginTop: 6, fontSize: 13 }}>
        <span className="muted">信息截止 </span>{s.data_cutoff || "—"}
        <span className="muted" style={{ marginLeft: 16 }}>信号就绪 </span>{s.signal_ready_at || "—"}
        <span className="muted" style={{ marginLeft: 16 }}>口径指纹 </span><code>{s.config_hash}</code>
      </div>
      <div style={{ marginTop: 4, fontSize: 13 }}>
        <span className="muted">信号 </span>{s.n_signals ?? "—"}
        <span className="muted" style={{ marginLeft: 16 }}>报价 </span>{s.n_quotes ?? "—"}
        <span className="muted" style={{ marginLeft: 16 }}>配对价格 </span>{s.n_priced ?? "—"}
        {s.degraded ? (
          <span className="badge warn" style={{ marginLeft: 16 }}>降级：{s.degrade_reasons}</span>
        ) : (
          <span className="badge ok" style={{ marginLeft: 16 }}>健康</span>
        )}
      </div>
    </div>
  );
}

function SignalTable() {
  const { data, err } = useData("/blindspot/signals");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;
  return (
    <>
      <h2>少数面信号榜（非交易信号）</h2>
      <div className="panel">
        <table>
          <thead><tr><th>品种</th><th>类型</th><th>分数</th><th>证据（摘要）</th></tr></thead>
          <tbody>
            {data.signals.map((s, i) => (
              <tr key={`${s.asof}-${s.variety}-${s.stype}-${i}`}>
                <td>{s.variety}</td>
                <td className="muted">{s.stype}</td>
                <td>{Number(s.score).toFixed(2)}</td>
                <td className="muted">{(s.evidence || "").slice(0, 100)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ItemTable() {
  const { data, err } = useData("/blindspot/items");
  if (err) return <ErrorBox msg={err} />;
  if (!data) return <Loading />;
  return (
    <>
      <h2>采集条目（含原文链接，按发布时间）</h2>
      <div className="panel">
        <table>
          <thead><tr><th>发布</th><th>类型</th><th>品种</th><th>标题</th></tr></thead>
          <tbody>
            {data.items.map((it) => (
              <tr key={it.id}>
                <td className="muted">{it.published || "—"}</td>
                <td className="muted">{it.kind}</td>
                <td>{(it.varieties || "[]").replace(/[\[\]\"]/g, "") || "—"}</td>
                <td><a href={it.url} target="_blank" rel="noreferrer">{it.title}</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function Blindspot() {
  return (
    <>
      <h1>少数面发现</h1>
      <Banner />
      <h2>数据同步状态</h2>
      <MetaPanel />
      <SnapshotPanel />
      <SignalTable />
      <ItemTable />
      <div className="muted" style={{ marginTop: 16, fontSize: 12 }}>
        数据由本地采集后同步至本系统 blindspot 独立分区；本页与行情/信号/预测/回测链路物理隔离。
      </div>
    </>
  );
}
