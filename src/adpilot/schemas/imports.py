"""导入的出参。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from adpilot.models.daily_metric import MetricLevel


class ImportResponse(BaseModel):
    """一次导入的结果摘要。

    **不回快照内容本身** —— 那可能是几千行，而调用方要的是「进去了多少、覆盖
    哪几天」。要看原始行去查 `raw_reports`。
    """

    model_config = ConfigDict(from_attributes=True)

    provider: str
    account_id: int

    #: 这份报表的投放层级。它是 `daily_metrics` 唯一键的一部分，回显出来是为了
    #: 让导入的人当场确认自己填对了 —— 填错不会报错，只会让数据挂到另一个层级上。
    level: MetricLevel

    #: 这份文件覆盖到的自然日（账户时区），升序
    days: list[date]

    #: 落进快照的数据行数
    rows: int

    #: 因为日期为空被跳过的行数。**正常情况下是 0 或 1** —— 平台导出的 CSV 末尾
    #: 常带一行「Total」汇总。这个数字明显偏大就说明文件不对，值得回头看一眼。
    skipped_rows: int
