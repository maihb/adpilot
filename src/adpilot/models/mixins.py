"""模型间共用的列。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """行的创建与最后更新时间，一律 `timestamptz`。

    两个列都给了 `server_default`，所以**不经 ORM 的写入**（迁移里的数据订正、
    批量 `INSERT ... ON CONFLICT`）也带得上时间，不会留下一片 NULL。

    ⚠️ `onupdate` 只在 ORM 发 UPDATE 时生效。归一化走的是 `ON CONFLICT DO
    UPDATE` 的批量 upsert，那条语句绕过 ORM，**必须在 `set_` 里显式带上
    `updated_at`** —— 漏了的症状是「数据明明重导过，updated_at 还停在上次」，
    而排查回填问题时看的正是这个列。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
