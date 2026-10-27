import app.backtest.level_touch as M
from app.backtest.level_touch.config import resolve_freq, DEFAULT_CONFIG

print("import OK")
print("daily:", {k: resolve_freq(DEFAULT_CONFIG)[k]
                for k in ["bs_freq", "warmup_bars", "eff_limit_excl", "z_every"]})
c = DEFAULT_CONFIG
c.freq = "60m"
print("60m  :", {k: resolve_freq(c)[k]
                for k in ["bs_freq", "warmup_bars", "eff_limit_excl", "z_every"]})
c2 = DEFAULT_CONFIG
c2.freq = "15m"
print("15m  :", {k: resolve_freq(c2)[k]
                for k in ["bs_freq", "warmup_bars", "eff_limit_excl", "z_every"]})
