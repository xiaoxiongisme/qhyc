import { useEffect, useState } from "react";
import { getJSON } from "./api.jsx";

export function useData(path, deps = []) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let alive = true;
    setData(null);
    setErr(null);
    getJSON(path)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setErr(e.message));
    return () => {
      alive = false;
    };
  }, deps);
  return { data, err };
}

export function Loading() {
  return <div className="loading">加载中…</div>;
}

export function ErrorBox({ msg }) {
  return <div className="panel err">加载失败：{msg}</div>;
}

export function ReadyBadge({ readiness }) {
  if (!readiness) return null;
  const ready = readiness.ready;
  return (
    <span className={"badge " + (ready ? "ok" : "warn")}>
      数据{ready ? "就绪" : "回补中"} {readiness.ready_ratio
        ? `${Math.round(readiness.ready_ratio * 100)}%`
        : readiness.ready_symbols !== undefined
        ? `${readiness.ready_symbols}/${readiness.target_symbols}`
        : ""}
    </span>
  );
}
