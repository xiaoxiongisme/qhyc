// 统一 API 客户端：同源部署（FastAPI 静态托管），相对路径即可
export async function getJSON(path) {
  const res = await fetch(path);
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
