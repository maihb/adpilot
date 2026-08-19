"""SQLAlchemy ORM 模型 —— **表结构的真相源**。

schema 怎么演进、autogenerate 有哪些盲区，见
[Schema 与迁移方案](../../../docs/design/2026-08-19-schema-migration.md)。

🔴 **新加一个模型，必须在这里 import 一次。** Alembic 比对的是
`Base.metadata`，而一个从没被 import 过的模块不会往里注册任何表 —— 症状是
`alembic revision --autogenerate` 跑得好好的、生成出来的迁移是空的，非常安静。
`migrations/env.py` 只 import 本模块，就是为了把「会不会被 import 到」收敛成
这一个地方。
"""

from __future__ import annotations

from adpilot.models.ad_account import AdAccount, Platform
from adpilot.models.client import Client
from adpilot.models.daily_metric import DailyMetric, MetricLevel

__all__ = [
    "AdAccount",
    "Client",
    "DailyMetric",
    "MetricLevel",
    "Platform",
]
