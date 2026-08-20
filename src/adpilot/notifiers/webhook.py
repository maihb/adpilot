"""通用 webhook：把一条告警 POST 到配置好的地址。

**没配就跳过，只记日志。** 这是刻意的默认行为：开源使用者未必有 webhook，而一个
「没配通知就起不来」的系统不符合「陌生人 clone 下来最容易跑起来」这条判断标准。
配了就推，配置项是 `ALERT_WEBHOOK_URL`（`config.py`）。

发的是**通用 JSON**，不迁就任何一家的消息格式。企业微信、钉钉、飞书各有各的
body 结构，在这里挑一家等于把其余几家挡在门外；通用 JSON 加一个中转（n8n、
自建小脚本、云函数）就能转成任何一家要的形状。真要原生支持某一家，那是在这个
目录下**新增一个 notifier**，不是改这个。
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import structlog
from pydantic import SecretStr

log = structlog.get_logger(__name__)

# 压得比默认的 5 秒还短。这一下是**巡检任务里的一次阻塞**，而通知是锦上添花 ——
# 对方慢，不能拖着巡检不放；反正推不出去下一轮还会重试（`notified_at` 留空）。
TIMEOUT_SECONDS: Final = 3.0


async def send(url: SecretStr, payload: dict[str, Any]) -> bool:
    """推一条出去，成功返回 `True`。

    🔴 **失败只记事件名和状态码，绝不记 URL** —— 它里面带着 key。日志是最容易被
    顺手贴进 issue 的东西，而贴出去的那一刻这个 webhook 就属于所有人了。

    不重试：巡检每小时跑一次，推失败的告警 `notified_at` 仍是空的，下一轮自然会
    再试一次。在这里加重试只会让一次网络抖动把巡检拖成分钟级。
    """
    target = url.get_secret_value()
    if not target:
        return False

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(target, json=payload)
    except httpx.HTTPError as exc:
        # 只记异常类名：httpx 的报错信息里会带上完整 URL。
        log.warning("alert_webhook_failed", error=type(exc).__name__)
        return False

    if response.is_success:
        return True

    log.warning("alert_webhook_rejected", status_code=response.status_code)
    return False
