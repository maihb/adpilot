"""MongoDB 客户端，存平台原始报表快照。

为什么要第二个库 —— 边界是：**钱和关系走 PostgreSQL，未经解释的原始事实走
MongoDB。**

广告平台会在 API 版本之间改字段名和结构，而今天拉的报表几个月后还得能查得到
（「当时这个数到底是多少？」）。所以 payload 原样落这里，append-only，永不原地
修改；归一化进 `daily_metrics` 是一次单向转换，映射规则变了或者发现 bug，随时
能拿这些快照重跑。

这个集合里的文档不会被更新。
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from adpilot.config import Settings

RAW_REPORTS = "raw_reports"

# motor 的类是按文档类型泛型化的。这里的文档就是普通 BSON 映射，起个别名，
# 下游所有标注都能短一截。
type MongoClient = AsyncIOMotorClient[dict[str, Any]]
type MongoDatabase = AsyncIOMotorDatabase[dict[str, Any]]


def create_client(settings: Settings) -> MongoClient:
    """构造异步 Mongo 客户端。

    `serverSelectionTimeoutMS` 特意压短：就绪探针应该一秒内报出「连不上」，
    而不是按默认值干等 30 秒。
    """
    return AsyncIOMotorClient(
        settings.mongo_uri.get_secret_value(),
        serverSelectionTimeoutMS=2000,
        tz_aware=True,
    )


def get_database(client: MongoClient, settings: Settings) -> MongoDatabase:
    return client[settings.mongo_db]
