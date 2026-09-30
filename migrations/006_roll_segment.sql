-- 等差后复权：换月分段表（替代「物化复权结果」）
--
-- 背景
-- ----
-- 前复权（锚定最新段）每次新增换月，全历史每根 bar 的复权值都会统一平移该跳空点数
-- （2026-09-30 实测：84 品种 10/10 命中），因此「存复权结果」必须全量重算，
-- 且本地 bar_* 历史一旦清理，重算就写不回历史（已有事故路径）。
--
-- 解法：改存「换月事件 + 分段累积偏移」。后复权锚定**最早段**，历史段偏移恒为 0，
-- 新换月只需 INSERT 一段，**历史永不改变**。
--
-- 为什么用等差（加减）而非等比（乘除）
-- ------------------------------------
-- 期货损益 = 点数差 × 合约乘数（线性）→ 必须让**点数**连续。
-- 实测 84 品种：等差 ATR 缩放恒等于 1.000（0% 品种失真）；
--               等比 87.8~90.2% 品种 ATR 失真（极端 0.032~31.1 倍）。
-- 等比虽能把负价品种降到 0，但会摧毁 2×ATR 止损/0.5×ATR 保本这类点位逻辑。
--
-- schema
-- ------
CREATE TABLE IF NOT EXISTS roll_segment (
    symbol      TEXT           NOT NULL,
    freq        TEXT           NOT NULL,          -- min5 / min15 / min30 / min60
    seg_no      INT            NOT NULL,          -- 段序号，0 = 最早段
    seg_start   TIMESTAMPTZ    NOT NULL,          -- 本段首根 bar（含）
    seg_end     TIMESTAMPTZ,                      -- 本段末根 bar 的**下一段起点**（不含）；NULL = 最后一段
    roll_ts     TIMESTAMPTZ,                      -- 进入本段的换月 bar；seg 0 为 NULL
    roll_delta  NUMERIC(20,4)  NOT NULL DEFAULT 0, -- 该次换月的 cc888 跳空点数；seg 0 为 0
    cum_offset  NUMERIC(20,4)  NOT NULL DEFAULT 0, -- **后复权累积偏移**（本段适用）
    price_shift NUMERIC(20,4)  NOT NULL DEFAULT 0, -- 整序列常量抬升（消除负价，见下）
    n_bars      INT,                              -- 本段 bar 数（数据质量校验用）
    src_freq    TEXT           NOT NULL DEFAULT 'min15', -- 换月检测的锚点周期
    updated_at  TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, freq, seg_no)
);

CREATE INDEX IF NOT EXISTS ix_roll_segment_lookup
    ON roll_segment (symbol, freq, seg_start);

COMMENT ON TABLE roll_segment IS
    '等差后复权分段表。adj(t) = 查 cum_offset；复权价 = raw + cum_offset。'
    '段 0（最早段）偏移为 0，历史永不改变，新换月只 INSERT 一段。';
COMMENT ON COLUMN roll_segment.cum_offset IS
    'cum_offset(k) = -Σ_{j<=k} roll_delta(j)。使跨换月的**点数差**归零：'
    '(raw_k_start + off_k) - (raw_{k-1}_end + off_{k-1}) = 0';
COMMENT ON COLUMN roll_segment.roll_delta IS
    '换月 bar 的 cc888 跳空点数（= close[i]-close[i-1]）。'
    '由 15m 双门检测定位，再按**包含关系**映射到目标 freq：取最后一个 '
    'seg_start <= 锚点时刻 的 bar（不可按 [锚点,锚点+15min) 找 bar，'
    '各周期 bucket 对齐不同会静默丢换月，见 scripts/build_roll_segments.py 注释）。';
