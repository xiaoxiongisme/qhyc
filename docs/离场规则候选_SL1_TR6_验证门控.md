# 离场规则候选 SL1.0 / TRAIL6.0 / BE0.5 —— 验证门控与落地说明

> 状态：**候选（未提升为默认）**。生产默认仍为 `sl_atr=2.0 / trail_atr=2.0 / be_r=0.5`。
> 来源建议：《实盘入场规则模拟报告_20261029.html》结论 + 用户拍板（R3）。

## 1. 建议是什么

以用户 **190 笔真实实盘入场点 + 真实合约小时线**套用 V2 离场规则做离场寻优，并对
"当前生产规格（分析口径 SL1.0 / TRAIL2.0 / BE0.5）"做 500 次随机留出，结论：

- 当前生产规格相对用户实际执行**系统性劣化**（削掉约 78% 收益）；
  根因是 **2×ATR 吊灯过紧、过早兑现浮盈**（MFE 兑现率由实际中位 20.6% 降至规则 0%）。
- 候选 **R3 = SL1.0 / TRAIL6.0 / BE0.5** 在留出检验中 **86~90% 优于实际执行**。

建议原文：**保持初始止损并将吊灯放宽至约 6×ATR，但必须经模拟盘 / 持续留出验证，
不能直接视为无条件生产规则。**

用户已拍板候选基准 = **R3（SL1.0 / TRAIL6.0 / BE0.5）**。

## 2. 口径澄清（重要）

分析建模时把"当前生产"当成 **SL1.0**，但生产 `FusionConfig` 真实默认是 **`sl_atr=2.0`**
（`app/core/config.py`）。因此：

- **候选 R3 相对生产默认** = 同时把 `sl_atr` 2.0→1.0、`trail_atr` 2.0→6.0、`be_r` 0.5 不变。
- 分析的 OOS 证据（86~90% 优于实际）建立在 **SL1.0/1.5 + TR6.0** 上；
  **SL2.0 + TR6.0 这一组合此前未被任何留出验证过**，需靠下方门控脚本确认。

这就是建议强调"先验证"的原因 —— 候选不是无条件规则。

## 3. 代码改动（默认不变，候选经环境变量启用）

文件：`app/core/config.py`

- 新增模块常量 `FUSION_EXIT_CANDIDATE = {"sl_atr": 1.0, "trail_atr": 6.0, "be_r": 0.5}`
  及完整背景注释。
- `Settings.fusion` 现经 `_apply_fusion_env_override()` 返回；**不设环境变量时完全沿用
  YAML / 默认**，对现有部署零影响。
- 环境变量（granular 优先于 profile，两者皆无则原样返回）：
  - `QH_FUSION_PROFILE=candidate` → 套用 `FUSION_EXIT_CANDIDATE`
  - `QH_FUSION_SL_ATR=1.0` / `QH_FUSION_TRAIL_ATR=6.0` / `QH_FUSION_BE_R=0.5` → 细粒度覆盖

> 回测与实时引擎**共用** `get_settings().fusion`（`fusion_params_from_config` 直接读它），
> 故 env 覆盖对两者同时生效 —— 同一套变量即可在模拟盘 / 影子进程里跑候选。

## 4. 如何部署候选（影子 / 模拟盘，不改默认）

在**独立**进程或容器里设置环境变量后启动实时引擎 / 回测，生产进程保持不设变量：

```bash
# 方式一：按名套用候选
export QH_FUSION_PROFILE=candidate

# 方式二：细粒度（等价）
export QH_FUSION_SL_ATR=1.0
export QH_FUSION_TRAIL_ATR=6.0
export QH_FUSION_BE_R=0.5

python -m app.main          # 或你的实时引擎入口
```

验证 env 是否生效：

```python
from app.core.config import get_settings
print(get_settings().fusion.sl_atr, get_settings().fusion.trail_atr, get_settings().fusion.be_r)
# 设了 candidate → (1.0, 6.0, 0.5)；未设 → (2.0, 2.0, 0.5)
```

## 5. 验证门控（"持续留出"的落地件）

脚本：`scripts/validate_exit_candidate.py`（在能连 DB 的环境运行，如 qhyc Docker 容器或本地带 PG 的 venv）

做两件事：
1. **walk-forward OOS**：在 V2 全品种 × 历史区间上滚动留出，比较
   基线（生产默认 SL2.0/TR2.0/BE0.5）与候选（SL1.0/TR6.0/BE0.5）的
   IS/OOS 净利、衰减比 `decay=OOS/IS`，给出判据。
2. **用户入场点复算**：若可定位 `live_final.json`（此前分析产物），直接报告候选(R3) 与
   基线(R1) 在用户真实入场点上的单笔均收益 / 胜率 / 金额。

晋升判据（**全部满足**才建议把默认切到候选）：

| 判据 | 含义 |
|---|---|
| A | 候选 OOS 净利 > 0 |
| B | 候选 decay ≥ 50%（OOS 未深度衰减） |
| C | 候选 OOS 净利 > 基线 OOS 净利 |
| D | 用户入场点样本上候选优于基线（与已有留出结论一致） |

运行：

```bash
# 全量（建议首次 & 定期）
python scripts/validate_exit_candidate.py
# 仅用户入场点复算
python scripts/validate_exit_candidate.py --no-wf
# 限定区间 / 抽样（快速）
python scripts/validate_exit_candidate.py --start 2020-01-01 --end 2026-10-01 --max-symbols 10
# 指定分析产物位置
LIVE_FINAL_JSON=/path/to/live_final.json python scripts/validate_exit_candidate.py
```

**持续留出**：把上面的全量命令挂到每日 / 每周定时任务（复用现有 cron 机制），
跨多段行情持续复核；只要 A~D 长期成立，再决定是否提升默认。

## 6. 当前状态与下一步

- ✅ 候选已可在影子 / 模拟盘部署（env 覆盖，默认未动）。
- ⏳ 待跑 `validate_exit_candidate.py` 全量，确认 A~D 成立。
- ⏳ 持续留出通过、且跨多段行情稳定后，再把 `FusionConfig` 默认由
  `sl_atr=2.0 / trail_atr=2.0` 改为 `1.0 / 6.0`（届时移除 env 覆盖即可）。
- ⚠ 注意：即便通过，候选仍可能在特定年份（如分析所示 2023 年小幅亏损、2025 年大幅盈利）
  不均衡，须保留对年度盈亏的监控，勿视为"永远无条件占优"。
