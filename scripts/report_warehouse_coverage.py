# -*- coding: utf-8 -*-
"""仓单日报 + 会员持仓汇总 数据覆盖率报告。

连接 prod PG，按交易所统计 warehouse_receipt 与 member_rank_summary 的
日期跨度、行数、交易日覆盖，并列出缺口（缺失的周一~周五）。
用法: python scripts/report_warehouse_coverage.py [--md out.md]
"""
import sys, os, argparse, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg2  # noqa: E402
import collect_warehouse_receipt as cwr  # noqa: E402


def weekdays(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        if d.isoweekday() <= 5:
            yield d
        d += dt.timedelta(days=1)


def exchange_report(cur, ex: str):
    cur.execute(
        "SELECT MIN(report_date), MAX(report_date), COUNT(DISTINCT report_date), SUM(1) "
        "FROM warehouse_receipt WHERE exchange=%s", (ex,))
    mn, mx, days, rows = cur.fetchone()
    if not mn:
        return None
    wd = sum(1 for _ in weekdays(mn, mx))
    present = set()
    cur.execute("SELECT DISTINCT report_date FROM warehouse_receipt WHERE exchange=%s", (ex,))
    for (d,) in cur.fetchall():
        present.add(d)
    gaps = [d for d in weekdays(mn, mx) if d not in present]
    # 近 30 日缺口（剔除明显节假日噪声，仅显示最近部分）
    return {
        "exchange": ex, "min": mn, "max": mx, "days": days, "rows": rows,
        "weekdays": wd, "coverage": days / wd if wd else 0, "gap_total": len(gaps),
        "gap_recent": [str(d) for d in gaps if (mx - d).days <= 60][-20:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", help="输出 markdown 报告路径")
    args = ap.parse_args()
    conn = psycopg2.connect(connect_timeout=10, **cwr.PG)
    out = []
    try:
        with conn, conn.cursor() as cur:
            out.append("# 仓单日报 / 会员持仓汇总 数据覆盖率报告")
            out.append(f"_生成时间: {dt.datetime.now():%Y-%m-%d %H:%M}_")
            out.append("")
            out.append("## warehouse_receipt（仓单日报）")
            out.append("")
            out.append("| 交易所 | 起始 | 截止 | 天数 | 行数 | 区间内工作日 | 覆盖率 | 缺口总数 |")
            out.append("|---|---|---|---|---|---|---|---|")
            for ex in ("SHFE", "CZCE", "DCE", "GFEX"):
                r = exchange_report(cur, ex)
                if not r:
                    out.append(f"| {ex} | — | — | 0 | 0 | — | — | — |")
                    continue
                out.append(
                    f"| {ex} | {r['min']} | {r['max']} | {r['days']} | {r['rows']:,} "
                    f"| {r['weekdays']} | {r['coverage']*100:.1f}% | {r['gap_total']} |")
            out.append("")
            out.append("### 各所近 60 日缺口（缺失的周一~周五，含部分节假日噪声）")
            out.append("")
            for ex in ("SHFE", "CZCE", "DCE", "GFEX"):
                r = exchange_report(cur, ex)
                if not r:
                    continue
                out.append(f"- **{ex}**: {r['gap_recent'] or '无'}")
            out.append("")
            out.append("## member_position_rank_summary（会员持仓汇总 top5/10/15/20）")
            out.append("")
            cur.execute(
                "SELECT MIN(report_date), MAX(report_date), COUNT(DISTINCT report_date), SUM(1) "
                "FROM member_position_rank_summary")
            mn, mx, days, rows = cur.fetchone()
            out.append(f"- 起始: {mn}  截止: {mx}  天数: {days}  行数: {rows:,}")
            out.append("")
            out.append("> 说明: warehouse_receipt 缺口统计含法定节假日（交易所休市但按周工作日计），")
            out.append("> 实际有效交易日覆盖率以交易所公告为准；SHFE 历史起点 2020-07-02，其余三所 2024-01-02。")
    finally:
        conn.close()
    text = "\n".join(out)
    print(text)
    if args.md:
        open(args.md, "w", encoding="utf-8").write(text)
        print(f"\n[m report] 已写入 {args.md}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
