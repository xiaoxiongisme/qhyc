"""TimescaleDB 压缩 chunk 安全回填（2026-10-08）。

## 为什么要这个模块

TimescaleDB 对**已压缩 chunk** 的直接 INSERT 会挂 `ts_insert_blocker` 触发器
（`_timescaledb_functions.insert_blocker()`）**静默丢弃**，`INSERT 0 n` 只报告
实际插入数。这是最典型的静默失效：命令成功、无报错、行数不对。

2026-10-07~08 两次实测踩中：
  1. ``l1_mkt.bar_5m`` 2026-03~06 缺口 318,965 行 —— 反复重插均 ``INSERT 0 0``，
     而 anti-join 统计一直是 318,965；
  2. ``l0_raw.daily_bar`` ZC888 181 行 —— 同因。

两者同一现象：``INSERT 0 0``（看似无新增可插）但键集比对显示大量缺失。
**根因就是压缩 chunk**。解压目标 chunk 后立刻 ``INSERT 0 318965`` / ``INSERT 0 345``
（与插入前 anti-join 预测值完全一致，形成闭环验证）。

另一个已踩过的坑：``public.daily_bar`` 是**视图**，真表在 ``l0_raw.daily_bar``。
往视图 INSERT 会 ``INSERT 0 0`` 且无任何报错。因此本模块**强制解析物理 schema**，
不信任调用方传入的表名。

## 设计取舍：为什么「逐 chunk 解压 → 压回」而非「全量解压」

实测（2026-10-08 云端）：1943 个 chunk 中 **1746 个已压缩**
（fut_kline 1121/1133、hourly_bar 348/350、bar_5m 135/144、minute_bar 142/144）。
磁盘现状：``l0_raw`` 2808MB / ``l2_adj`` 1336MB / 库合计 18GB / 磁盘剩 37G。

- 全量解压磁盘代价不可接受（``minute_bar`` 1.19 亿行）；
- 逐 chunk 解压 → 插入 → 压回，**磁盘峰值只多一个 chunk**；
- 单 chunk 解压/压回实测**秒级**（``daily_bar`` chunk 各 0.08s），
  相对回填本身的 COPY/INSERT 可忽略。

故本模块按 chunk 粒度循环，天然错峰（可选 ``batch_chunks`` 合并少量相邻 chunk
以减少解压/压缩往返次数）。

## 用法

    from app.db.timescale_guard import backfill_window

    with backfill_window(cur, "daily_bar", start, end) as ctx:
        # 块内目标 chunk 已解压，可安全 INSERT（注意用物理 schema）
        cur.execute("INSERT INTO l0_raw.daily_bar ... ON CONFLICT DO NOTHING")
        ctx.expected_inserted = 181
        ctx.check(cur.rowcount)          # 不足即抛 InsertShortfall
    # 退出 with 时自动压回，磁盘回到原状

## 断言为何必须 raise 而非 warning

静默丢数是本项目最主要的缺陷类型。若这里只 log warning，回填照样「成功」——
等于没修。因此**断言失败直接抛异常**，由调用方回滚事务 / job 记录失败。
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterator

from app.core.logging import logger

#: 物理表候选 schema（按优先级）。G4 分层后真表在这些 schema 下，
#: ``public.*`` 多为同名视图 —— 往视图写会静默无效果，故必须先解析。
PHYSICAL_SCHEMAS = ("l0_raw", "l1_mkt", "l2_adj", "l3_ref", "public")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str, what: str) -> str:
    """表名/列名只允许裸标识符。

    表名会拼进 DDL/SQL（``compress_chunk(..., '<chunk>'::regclass)``），
    虽来自 ``information_schema`` 查询结果，但**校验后再用**可挡住意外输入。
    """
    if not _IDENT.match(name or ""):
        raise ValueError(f"[timescale_guard] 非法{what}: {name!r}")
    return name


def _exec(cur, sql: str, params: tuple = ()):
    """在 psycopg2 游标 / SQLAlchemy Connection 上都能执行 SQL。

    两点兼容（2026-10-08 实测）：
      - SQLAlchemy 2.x 不把裸字符串当可执行语句（``ObjectNotExecutableError``），
        需经 ``exec_driver_sql``；
      - ``exec_driver_sql`` 走 psycopg2 驱动，**不解析 ``:name``**，只认 ``%s``。
        故统一用 ``%s`` 位置占位，两种游标通用。
    """
    if hasattr(cur, "exec_driver_sql"):
        return cur.exec_driver_sql(sql, params)
    return cur.execute(sql, params)


def _fetchall(cur, sql: str, params: tuple = ()) -> list[tuple]:
    """执行并取全部行，兼容 psycopg2 cursor（``cur.fetchall()``）与
    SQLAlchemy Connection（结果在返回的 ``CursorResult`` 里，Connection 本身
    没有 ``fetchall``）。"""
    res = _exec(cur, sql, params)
    if hasattr(res, "fetchall"):          # psycopg2 cursor
        return res.fetchall()
    return list(res.fetchall())           # SQLAlchemy CursorResult


def _fetchone(cur, sql: str, params: tuple = ()):
    """执行并取第一行（两种游标通用）。"""
    res = _exec(cur, sql, params)
    if hasattr(res, "fetchone"):
        return res.fetchone()
    row = res.fetchone()
    return row


def resolve_physical(cur, table: str) -> str:
    """解析表名对应的**物理 schema**（跳过同名视图）。

    ★ 2026-10-08：``public.daily_bar`` 是 VIEW、真表在 ``l0_raw.daily_bar``。
      往视图 INSERT 会 ``INSERT 0 0`` 且**不报错**（实测），故所有写路径
      必须先过这里，不能直接用调用方给的 ``public.xxx``。
    """
    _validate_ident(table, "表名")
    rows = _fetchall(
        cur,
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = %s AND c.relkind IN ('r','p')",
        (table,),
    )
    if not rows:
        raise ValueError(f"[timescale_guard] {table} 无物理表（不存在或仅是视图）")
    order = {s: i for i, s in enumerate(PHYSICAL_SCHEMAS)}
    rows.sort(key=lambda r: order.get(r[0], 99))
    return rows[0][0]


def is_hypertable(cur, schema: str, table: str) -> bool:
    row = _fetchone(
        cur,
        "SELECT EXISTS(SELECT 1 FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema=%s AND hypertable_name=%s)",
        (schema, table),
    )
    return bool(row[0])


def _to_ts(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return f"{v.isoformat()} 00:00:00"
    return str(v).replace("T", " ")


def chunks_in_range(cur, schema: str, table: str, start: Any, end: Any):
    """返回与 [start, end) 重叠的 chunk：(chunk_schema, chunk_name, is_compressed)。"""
    rows = _fetchall(
        cur,
        "SELECT chunk_schema, chunk_name, is_compressed "
        "FROM timescaledb_information.chunks "
        "WHERE hypertable_schema=%s AND hypertable_name=%s "
        "  AND range_end > %s AND range_start < %s "
        "ORDER BY range_start",
        (schema, table, _to_ts(start), _to_ts(end)),
    )
    return [(r[0], r[1], bool(r[2])) for r in rows]


def decompress_chunk(cur, chunk_schema: str, chunk_name: str) -> None:
    """解压单个 chunk（幂等）。

    两个易错点（2026-10-08 实测）：
      - 签名是 ``decompress_chunk(regclass, boolean)``，**没有**命名参数
        ``if_not_compressed=>``（本版本不兼容，报 "No function matches"）；
      - chunk 位于 ``_timescaledb_internal`` schema，**不是** hypertable 所在
        schema（用 ``l1_mkt.<chunk>`` 报 "relation does not exist"）。
    """
    _exec(cur, "SELECT decompress_chunk(%s::regclass, true)",
          (f"{chunk_schema}.{chunk_name}",))


def compress_chunk(cur, chunk_schema: str, chunk_name: str) -> None:
    _exec(cur, "SELECT compress_chunk(%s::regclass, true)",
          (f"{chunk_schema}.{chunk_name}",))


@dataclass
class BackfillResult:
    """回填窗口的执行结果（用于日志与断言追溯）。"""

    table: str
    schema: str
    chunks_seen: int = 0
    chunks_decompressed: int = 0
    chunks_recompressed: int = 0
    expected_inserted: int = 0
    actual_inserted: int = 0      # 本组（每次 yield 归零）
    total_inserted: int = 0       # 累计（仅日志用）

    @property
    def ok(self) -> bool:
        return self.total_inserted >= self.expected_inserted

    def summary(self) -> str:
        return (
            f"[{self.schema}.{self.table}] chunk {self.chunks_seen}"
            f"(解压 {self.chunks_decompressed}/压回 {self.chunks_recompressed}) "
            f"本组 {self.actual_inserted}/{self.expected_inserted} 累计 {self.total_inserted}"
        )

    def check(self, actual: int, minimum: int | None = None) -> None:
        """断言**本组**插入行数；不足即抛 :class:`InsertShortfall`。

        ★ 按组独立计数（2026-10-08 修正）：首版用跨组累加，导致第 2 组插入 0 行时
        仍被第 1 组的累计值「顶过」而假通过 —— 断言形同虚设。
        ``backfill_chunks`` 每次 yield 前会把 ``actual_inserted`` 归零，
        故这里默认就是「本组」语义；``summary()`` 另附累计值便于溯源。
        """
        self.actual_inserted += actual
        self.total_inserted += actual
        need = self.expected_inserted if minimum is None else minimum
        if self.actual_inserted < need:
            raise InsertShortfall(
                f"[timescale_guard] 插入行数不足：本组实际 {self.actual_inserted} < 预期 {need}。"
                f"最可能原因是目标 chunk 仍处压缩态被静默拦截。{self.summary()}"
            )


class InsertShortfall(AssertionError):
    """插入行数低于预期 —— **必须让调用方事务回滚**，不能只记日志。

    这是本模块存在的核心目的：TimescaleDB 压缩 chunk 会静默丢数，
    没有断言就会「看起来成功」。
    """


@contextmanager
def backfill_window(cur, table: str, start: Any, end: Any, *,
                    compress_back: bool = True) -> Iterator[BackfillResult]:
    """**单组**安全回填窗口：解压 [start, end) 内的全部 chunk → 交你插入 → 压回。

    只 yield 一次（``with`` 语义要求）。若区间内 chunk 很多、想按组轮转以压低
    磁盘峰值，请改用 :func:`backfill_chunks`（生成器，可多次 yield）。

    非超表、或区间内无 chunk 时也会 yield 一次（此时不解压，直接可用）。
    """
    gen = backfill_chunks(cur, table, start, end, compress_back=compress_back,
                          batch_chunks=10 ** 6)   # 极大 = 全部一批
    try:
        result = next(gen)
    except StopIteration:
        return
    yield result
    # 让生成器走到末尾以触发压回
    for _ in gen:
        pass


def backfill_chunks(cur, table: str, start: Any, end: Any, *,
                    compress_back: bool = True,
                    batch_chunks: int = 1) -> Iterator[BackfillResult]:
    """逐组安全回填（**生成器**）：解压一组 chunk → 交你插入 → 压回 → 下一组。

    正确用法（每组都必须用完，否则该组 chunk 不会被压回）::

        for ctx in backfill_chunks(cur, "bar_5m", start, end):
            cur.execute("INSERT ...")
            ctx.expected_inserted = n
            ctx.check(cur.rowcount)

    为什么用生成器而不是 ``with``：一个 chunk 组一次 ``yield``，而上下文管理器
    只能 yield 一次（2026-10-08 首版即踩到 ``generator didn't stop``）。
    生成器天然支持「一组一轮」，且在调用方异常退出时也能回收已解压的 chunk。

    参数
    ----
    cur:
        已在事务中的游标。**断言失败抛异常，由调用方决定回滚**——本生成器
        刻意不自行 commit/rollback，避免与调用方事务边界混淆。
    batch_chunks:
        每组几个相邻 chunk。1 = 磁盘峰值最低；调大可减少解压/压缩往返
        （每次都是 DDL 事务）。默认 1。
    compress_back:
        每组用完后是否压回。**默认 True**，否则磁盘不会恢复。
    """
    schema = resolve_physical(cur, table)
    result = BackfillResult(table=table, schema=schema)

    if not is_hypertable(cur, schema, table):
        logger.info(f"[timescale_guard] {schema}.{table} 非超表，跳过 chunk 处理")
        yield result
        return

    all_chunks = chunks_in_range(cur, schema, table, start, end)
    result.chunks_seen = len(all_chunks)
    if not all_chunks:
        logger.info(f"[timescale_guard] {schema}.{table} [{start}~{end}) 无 chunk，跳过")
        yield result
        return

    n = max(1, int(batch_chunks))
    opened: list[tuple[str, str]] = []
    try:
        for i in range(0, len(all_chunks), n):
            for cs, cn, comp in all_chunks[i:i + n]:
                if comp:
                    decompress_chunk(cur, cs, cn)
                    opened.append((cs, cn))
                    result.chunks_decompressed += 1
            result.actual_inserted = 0      # 本组计数归零（见 check 的说明）
            yield result                     # ← 调用方在此时做插入
            if compress_back:                # 恢复点后（即本组被用完）压回
                for cs, cn in opened:
                    try:
                        compress_chunk(cur, cs, cn)
                        result.chunks_recompressed += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            f"[timescale_guard] 压回 {cs}.{cn} 失败（磁盘未回收，"
                            f"请稍后手动 compress_chunk）: {exc}")
                opened.clear()
    except Exception:                        # 异常路径：尽力回收磁盘
        for cs, cn in reversed(opened):
            try:
                compress_chunk(cur, cs, cn)
                result.chunks_recompressed += 1
            except Exception:  # noqa: BLE001
                logger.warning(f"[timescale_guard] 异常路径压回 {cs}.{cn} 失败")
        raise
    finally:
        logger.info(result.summary())

