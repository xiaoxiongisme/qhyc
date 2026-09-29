"""
API 安全层（评估文档 §8.1 / P0#3：API 鉴权中间件 + 替换占位符 Key）

提供 X-API-Key 校验依赖 ``verify_api_key``，供路由以 ``Depends`` 挂载。

设计原则（防止「上线即打挂生产」）：
- 仅当部署侧在 ``.env`` 中配置了真实 ``INTEGRATION_API_KEY``（非空、非占位符）时，
  才真正强制校验；否则退化为「放行」（passthrough）。
- 这样「是否启用鉴权」完全由部署侧控制：云端填上真实 key 立即生效，
  未填则行为与改动前完全一致 → 代码侧零风险上线。

参考：评估文档 §8.1。
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# 视为「未启用鉴权」的占位符清单（含历史遗留占位符，避免把已知弱值当真密钥强制）
_PLACEHOLDER_KEYS = {
    "", "changeme", "your-api-key-here", "FILL_ME_IN", "placeholder",
    "dev_placeholder", "dev_placeholder_change_before_m5",
}


def auth_enabled() -> bool:
    """部署侧是否配置了真实 API Key（对外暴露，供 main.py 决定 /docs 是否开放）。"""
    key = (get_settings().env.INTEGRATION_API_KEY or "").strip()
    return key not in _PLACEHOLDER_KEYS


# 兼容旧名（内部使用）
_auth_enabled = auth_enabled


async def verify_api_key(key: str | None = Security(_api_key_header)) -> str | None:
    """API Key 校验依赖。

    返回有效 key（供路由使用）；未启用鉴权时返回 ``None``（放行）；
    已启用但缺失/不匹配时抛 ``403``。
    """
    if not auth_enabled():
        return None  # 鉴权未启用：放行，保持向后兼容
    expected = (get_settings().env.INTEGRATION_API_KEY or "").strip()
    # 常量时间比较，避免按字节逐位比对泄露 key 前缀（计时侧信道）
    if key is None or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    return key
