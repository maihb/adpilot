"""Redis 客户端。

本项目里它干两件事：

* **限流。** Meta 和 TikTok 都对报表 API 限速，而一次被限速后静默丢掉一天数据，
  比跑得慢糟糕得多。令牌桶放这里，多个 worker 共享同一份按账户的配额。
* **热点指标缓存。** 客户端看板整天反复读同样几个聚合值，没必要每次都回数据库。
"""

from __future__ import annotations

from redis.asyncio import Redis

from adpilot.config import Settings


def create_client(settings: Settings) -> Redis:
    """构造异步 Redis 客户端。

    `decode_responses=True` 是因为这里存的全是文本或 JSON；给调用方丢裸 bytes
    只会让每个调用点都挂一句 `.decode()`。
    """
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=2,
    )
