"""告警巡检任务：定时把规则跑一遍，对账进 `alerts` 表。

编排在这里，逻辑在 `services/alert.py` —— 和归一化那个任务同一个形状。

**这是第一个由 beat 触发、而不是由人的动作触发的任务。** 排期在
`db/broker.py` 的 `beat_schedule` 里，跑起来要有一个 beat 进程（`make beat`，
或者 compose 里的 `beat` 服务）。没有它的话，这个任务只是躺在那儿，等着谁手动投
一条 —— 而「忘了起 beat」的症状是「告警一条都不来」，看起来跟「一切正常」一样。
"""

from __future__ import annotations

import structlog
from celery.exceptions import Reject

from adpilot.config import get_settings
from adpilot.db.broker import TASK_SWEEP_ALERTS
from adpilot.db.postgres import transaction
from adpilot.resources import Resources
from adpilot.services import alert as alert_service
from adpilot.services.exceptions import DomainError
from adpilot.tasks import runtime
from adpilot.tasks.app import AdpilotTask, celery_app

log = structlog.get_logger(__name__)


@celery_app.task(base=AdpilotTask, bind=True, name=TASK_SWEEP_ALERTS)
def sweep_alerts(self: AdpilotTask) -> dict[str, object]:
    """巡检一遍所有在投账户的规则。

    **没有参数，也不需要。** 巡检的输入就是「库里此刻的样子」，传一个时间点进来
    只会带来「补跑昨天」这种目前没人需要的语义，以及一个会被序列化搞错的日期。

    可以反复跑：对账是幂等的（同一件事只有一条 `open`），所以它敢挂在每小时的排期
    上，也敢在失败后重试。
    """
    try:
        summary = runtime.run(_sweep)
    except DomainError as exc:
        # 走到这里说明数据本身有问题（比如账户时区填了个不存在的名字），重试
        # 一万次还是同一个错。进死信队列等人看。
        log.error("alert_sweep_rejected", task_id=self.request.id, reason=exc.message)
        raise Reject(exc.message, requeue=False) from exc

    return {
        "accounts": summary.accounts,
        "opened": summary.opened,
        "still_open": summary.still_open,
        "resolved": summary.resolved,
        "notified": summary.notified,
    }


async def _sweep(resources: Resources) -> alert_service.SweepSummary:
    async with transaction(resources.session_factory) as session:
        return await alert_service.sweep(session, get_settings())
