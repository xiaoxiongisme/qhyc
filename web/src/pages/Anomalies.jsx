// 数据质量 · 异常工单面板（PRD §10「数据质量面板」补齐）
//
// 后端：GET /anomalies?status=pending&limit=200（返回数组）
//       POST /anomalies/{id}/resolve  body={action: accept_tqsdk|false_positive, note}
// 纪律：只允许「采纳 tqsdk」或「标记误报」，不手改数值（后端已强制）。
import React, { useCallback, useEffect, useState } from "react";
import { getJSON, postJSON } from "../api.jsx";

const STATUS_LABEL = {
  pending: "待裁决",
  fixed: "已修正",
  false_positive: "误报",
};

export default function Anomalies() {
  const [items, setItems] = useState([]);
  const [status, setStatus] = useState("pending");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const d = await getJSON(`/anomalies?status=${status}&limit=200`);
      setItems(Array.isArray(d) ? d : d.items || []);
    } catch (e) {
      setErr(String(e.message || e));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    load();
  }, [load]);

  async function resolve(id, action) {
    setBusyId(id);
    try {
      await postJSON(`/anomalies/${id}/resolve`, { action, note: "via dashboard" });
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="card">
      <h2>数据质量 · 异常工单</h2>
      <div className="small">
        双源（akshare / tqsdk）不一致工单。裁决仅允许「采纳 tqsdk」或「标记误报」，禁止手改数值。
      </div>
      <div className="tabs" style={{ marginTop: 8 }}>
        {["pending", "all", "fixed", "false_positive"].map((s) => (
          <button
            key={s}
            className={status === s ? "active" : ""}
            onClick={() => setStatus(s)}
          >
            {STATUS_LABEL[s] || s}
          </button>
        ))}
        <button onClick={load}>刷新</button>
      </div>

      {err && <div className="err">错误：{err}</div>}
      {loading ? (
        <div className="small">加载中…</div>
      ) : items.length === 0 ? (
        <div className="small">该状态下无工单。</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>品种</th>
              <th>交易日</th>
              <th>字段</th>
              <th>akshare</th>
              <th>tqsdk</th>
              <th>差异</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.id}>
                <td>{t.id}</td>
                <td>{t.symbol}</td>
                <td>{t.trade_date}</td>
                <td>{t.field}</td>
                <td>{t.akshare_val ?? "—"}</td>
                <td>{t.tqsdk_val ?? "—"}</td>
                <td>{t.diff ?? "—"}</td>
                <td>{STATUS_LABEL[t.status] || t.status}</td>
                <td>
                  {t.status === "pending" ? (
                    <>
                      <button
                        disabled={busyId === t.id}
                        onClick={() => resolve(t.id, "accept_tqsdk")}
                      >
                        采纳 tqsdk
                      </button>{" "}
                      <button
                        disabled={busyId === t.id}
                        onClick={() => resolve(t.id, "false_positive")}
                      >
                        误报
                      </button>
                    </>
                  ) : (
                    <span className="small">已裁决</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
