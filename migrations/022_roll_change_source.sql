-- 022_roll_change_source.sql
-- G2（评审 §2.1，P0）：**统一换月真源**。
--
-- 问题：`main_contract_map.change_flag`（按合约符号变）与 `roll_segment`（按价格双门）
--        两套"换月"定义**独立生成**。若主力切换走"持仓量渐进交叉"（价格不跳），
--        change_flag 触发但 roll_segment 不触发 → 回测执行序列换了合约、后复权序列却
--        无 offset 边界 → **信号/执行在换月处对不齐**。评审定义为「比双轨更底层的
--        根因级漏洞」。
--
-- 处置：以 `main_contract_map.change_flag` 为换月事件**唯一真源**；
--       双门（振幅 + 8888 比率）降级为**校验/告警**：
--         价格有跳变却无 change 点  → 记 anomaly_ticket（不擅自造 offset）
--         有 change 点却无价格跳变  → 记 anomaly_ticket（正常：渐进换月本就不跳）
--
-- 本迁移只加**可审计列**，不改既有段数据（append_only，历史永不重算）：
--   * change_source : 该段边界来自何处（MAIN_MAP / DUAL_GATE_FALLBACK）
--   * contract_code : 该段对应的真实合约（G2 附带补齐 —— 此前 roll_segment 只存
--                     roll_delta/cum_offset，不存每段对应哪个合约，导致反解无法对齐
--                     口径，是 MA 一致性闸 13.86% 阻断的数据模型缺口）

ALTER TABLE roll_segment
    ADD COLUMN IF NOT EXISTS change_source text;

ALTER TABLE roll_segment
    ADD COLUMN IF NOT EXISTS contract_code text;

COMMENT ON COLUMN roll_segment.change_source IS
    '段边界来源：MAIN_MAP=由 main_contract_map.change_flag 派生（真源）；'
    'DUAL_GATE_FALLBACK=无 change 点时的双门兜底（应告警并逐步消灭）';

COMMENT ON COLUMN roll_segment.contract_code IS
    '该段对应的真实合约码（如 MA2611）。补齐「连续段↔真实合约」的权威映射，'
    '供反解与一致性闸对齐口径（G2）';

CREATE INDEX IF NOT EXISTS ix_roll_segment_contract
    ON roll_segment (symbol, freq, contract_code);
