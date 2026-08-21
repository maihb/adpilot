"""定时日报任务：把此刻该出的那几份日报生成出来。

编排在这里，判定和生成在 `services/report.py` —— 和告警巡检那个任务同一个形状。

**这是第二个由 beat 触发的任务**（第一个是告警巡检）。排期在 `db/broker.py` 的
`beat_schedule` 里，错开在每小时的第 20 分 —— 让巡检先跑完，因为新开的告警正是
日报要引用的东西。

## 🔴 它和巡检有一处关键不同：这个任务会花钱

巡检是纯数据库操作，跑一万次也只是几条 SQL。这个任务每生成一份日报就是一次 LLM
调用。所以：

* **判定必须便宜**：`due_reports()` 不写任何东西，且绝大多数轮次它返回空列表
  （那天已经有日报了）—— 密集排期靠的是这个，不是靠限流；
* **每份各自一个事务**：一份失败不能让前面已经生成的那些一起回滚，否则下一轮要
  重新烧一遍那些调用；
* **额度用光就整轮中断**，不是跳过继续 —— 继续只会把剩下的调用全撞在同一堵墙上。

理由的完整版在[定时日报设计](../../../docs/design/2026-08-21-scheduled-reports.md)
第七节。
"""

from __future__ import annotations

import structlog
from celery.exceptions import Reject

from adpilot.config import get_settings
from adpilot.db.broker import TASK_GENERATE_DUE_REPORTS
from adpilot.resources import Resources
from adpilot.services import report as report_service
from adpilot.services.exceptions import DomainError
from adpilot.tasks import runtime
from adpilot.tasks.app import AdpilotTask, celery_app

log = structlog.get_logger(__name__)


@celery_app.task(base=AdpilotTask, bind=True, name=TASK_GENERATE_DUE_REPORTS)
def generate_due_reports(self: AdpilotTask) -> dict[str, object]:
    """给每个「该出日报但还没出」的 (账户, 日期) 各生成一份 **draft**。

    **没有参数，也不需要** —— 输入就是「库里此刻的样子」，同巡检那条。

    可以反复跑：判定的第三个条件就是「那天还没有任何日报」，所以同一份不会生成
    两次。这是它敢挂在每小时排期上的前提。

    🔴 **生成出来的是草稿，绝不自动发布。** 发布的两条硬校验（人工修订过、操作
    记录非空）是「LLM 只解释不决策」那条边界的最后一道人工闸门 —— 模型可能在散文
    里编一个百分比，而没有机器拦得住它。
    """
    try:
        summary = runtime.run(_generate)
    except DomainError as exc:
        # 走到这里说明**判定阶段**就出了问题（比如某个账户的时区名不存在），
        # 而不是某一份日报生成失败 —— 后者在服务层里被逐个吞掉并计进 `failed`。
        # 判定阶段的错重试一万次还是同一个，进死信队列等人看。
        log.error("scheduled_reports_rejected", task_id=self.request.id, reason=exc.message)
        raise Reject(exc.message, requeue=False) from exc

    return {
        "accounts": summary.accounts,
        "generated": summary.generated,
        "failed": summary.failed,
        "quota_exhausted": summary.quota_exhausted,
    }


async def _generate(resources: Resources) -> report_service.ScheduleSummary:
    # 🔴 传的是 **session_factory 而不是 session**：每份日报各自一个事务
    # （`services/report.py` 的 `generate_due` 讲了为什么）。这也是这个任务体
    # 里唯一一处和巡检那个不同的地方。
    return await report_service.generate_due(resources.session_factory, get_settings())
