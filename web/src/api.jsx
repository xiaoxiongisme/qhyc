// 统一 API 客户端：同源部署（FastAPI 静态托管），相对路径即可
//
// ⚠ 鉴权（整改优先级 P2 / PRD §9）：后端除 /health 外的全部路由都要求
//   `X-API-Key` 头（守卫式：仅当 .env 配了真实 INTEGRATION_API_KEY 才强制）。
//   前端必须带上该头，否则看板所有接口都会 403。
//   构建期注入：VITE_API_KEY（见 web/.env.example）。
const API_KEY = import.meta.env.VITE_API_KEY || "";

export function authHeaders(extra = {}) {
  return API_KEY ? { "X-API-Key": API_KEY, ...extra } : { ...extra };
}

export async function getJSON(path) {
  const res = await fetch(path, { headers: authHeaders() });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export async function postJSON(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(digits)}%`;
}

export function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(digits);
}

export function dirClass(direction) {
  // A 股习惯：红涨绿跌
  return direction === "up" ? "dir-up" : "dir-down";
}

export function dirText(direction) {
  return direction === "up" ? "看涨 ▲" : "看跌 ▼";
}

export function retClass(v) {
  if (v === null || v === undefined) return "";
  return v > 0 ? "up" : v < 0 ? "down" : "";
}

export function caliberBadge(caliber) {
  // §17 工单④：所有涨跌展示标注收盘价口径
  const label = caliber === "settle" ? "结算价口径" : "收盘价口径";
  return <span className="badge caliber">{label}</span>;
}
