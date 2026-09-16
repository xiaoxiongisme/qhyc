"""
推送通道（微信 pushplus / 备用 webhook）

主通道：pushplus（推送加）
- 端点：https://www.pushplus.plus/send
- 调用：POST application/json，字段 token / title / content / template
- template 用 "markdown"（融合信号内容为 markdown）
- 失败自动重试一次（网络抖动/限流），仍失败返回 False

备用通道：通用 webhook（企业微信 / 钉钉 / 飞书 群机器人）
- 用 fusion.fallback_webhook 配置；主通道失败时自动降级
- 企业微信 {"msgtype":"markdown","markdown":{"content":...}}
- 钉钉    {"msgtype":"markdown","markdown":{"title":...,"text":...}}
- 飞书    {"msg_type":"text","content":{"text":...}}（飞书机器人不带标题，标题并入正文）
自动按 URL 域名选择载荷格式。

推送是旁路：任何失败只记日志、不抛异常，绝不影响调度主流程。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from app.core.config import get_settings
from app.core.logging import logger

_ENDPOINT = "https://www.pushplus.plus/send"


def _post_json(url: str, payload: dict, timeout: int = 10) -> tuple[bool, str]:
    """POST JSON，返回 (是否 HTTP 成功, 响应正文)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return 200 <= getattr(resp, "status", 200) < 300, body
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _send_pushplus_once(key: str, title: str, content: str) -> bool:
    payload = {
        "token": key,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    ok, body = _post_json(_ENDPOINT, payload)
    if not ok:
        logger.warning(f"[pushplus] HTTP 失败: {body[:200]}")
        return False
    try:
        js = json.loads(body)
    except Exception:
        logger.warning(f"[pushplus] 响应非 JSON: {body[:200]}")
        return False
    # pushplus 成功返回 code=200（部分镜像返回 0），其余为错误码
    if js.get("code") in (200, 0):
        return True
    logger.warning(f"[pushplus] 推送返回错误 code={js.get('code')} msg={js.get('msg')}")
    return False


def send_pushplus(title: str, content: str, token: str | None = None,
                  retries: int = 1) -> bool:
    """推送一条消息到微信（pushplus）。失败自动重试 `retries` 次。返回是否成功。"""
    key = token or get_settings().pushplus_token
    if not key:
        logger.warning(
            "[pushplus] TOKEN 未配置，跳过推送"
            "（可在 .env 的 PUSHPLUS_TOKEN 或 local.yaml 的 fusion.pushplus_token 填写）"
        )
        return False

    for attempt in range(retries + 1):
        if _send_pushplus_once(key, title, content):
            logger.info(f"[pushplus] 推送成功: {title}")
            return True
        if attempt < retries:
            time.sleep(1.5)  # 短暂退避后重试（网络抖动/限流）
    logger.warning(f"[pushplus] 重试 {retries} 次后仍失败: {title}")
    return False


def _webhook_payload(url: str, title: str, content: str) -> dict:
    """按 webhook 域名选择载荷格式（企业微信 / 钉钉 / 飞书）。"""
    host = urllib.parse.urlparse(url).netloc.lower()
    if "feishu" in host or "larksuite" in host:
        # 飞书机器人没有独立标题字段，标题并进正文
        return {"msg_type": "text", "content": {"text": f"{title}\n{content}"}}
    if "dingtalk" in host:
        return {"msgtype": "markdown", "markdown": {"title": title, "text": content}}
    # 企业微信群机器人（默认）
    return {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{content}"}}


def send_webhook(url: str, title: str, content: str) -> bool:
    """备用通道：企业微信/钉钉/飞书群机器人。返回是否成功。"""
    if not url:
        return False
    ok, body = _post_json(url, _webhook_payload(url, title, content))
    if not ok:
        logger.warning(f"[webhook] HTTP 失败: {body[:200]}")
        return False
    low = body.lower()
    # 三家成功都返回 errcode/StatusCode 为 0
    if '"errcode":0' in low.replace(" ", "") or '"statuscode":0' in low.replace(" ", ""):
        logger.info(f"[webhook] 备用通道推送成功: {title}")
        return True
    logger.warning(f"[webhook] 返回异常: {body[:200]}")
    return False


def send_notify(title: str, content: str, fallback_webhook: str = "") -> tuple[bool, str]:
    """统一发送入口：主通道 pushplus → 失败降级备用 webhook。

    返回 (是否送达, 实际通道名)。通道名用于留痕/统计。
    """
    if send_pushplus(title, content):
        return True, "pushplus"
    if fallback_webhook:
        logger.warning("[notify] pushplus 失败，降级备用 webhook")
        if send_webhook(fallback_webhook, title, content):
            return True, "webhook"
    return False, "none"


__all__ = ["send_pushplus", "send_webhook", "send_notify"]
