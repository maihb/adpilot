"""告警的路由。

`/alerts` 下面挂着两类东西，语义不同、都有用：

* **`/alerts`** —— 巡检落表的告警。回答「有哪些问题还没解决、各自拖了多久」，
  它是待办清单，也是历史。
* **`/alerts/balances`** —— 余额可撑天数的**现算**清单。回答「此刻各账户是什么
  状况」，包括那些还没到告警阈值的。

两者不是一个东西的两种写法：前者带状态和时间线，后者是一张即时快照。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.balance import to_runway_response
from adpilot.api.deps import SessionDep, SettingsDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.models.alert import AlertStatus
from adpilot.schemas.alert import (
    AlertItem,
    AlertListResponse,
    DiagnosisItem,
    DiagnosisResponse,
    SweepResponse,
)
from adpilot.schemas.balance import BalanceAlertListResponse
from adpilot.services import alert as alert_service
from adpilot.services import balance as balance_service

router = APIRouter(tags=["alerts"])


@router.get(
    "/alerts",
    response_model=AlertListResponse,
    operation_id="listAlerts",
)
async def list_alerts(
    session: SessionDep,
    alert_status: Annotated[
        AlertStatus | None,
        Query(alias="status", description="只看某个状态；不给就是全部"),
    ] = None,
    account_id: Annotated[int | None, Query(description="只看某个账户")] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
) -> AlertListResponse:
    """列出巡检发现的告警，**未解决的排在最前面**。

    排序不是单纯按时间倒序：这张表既是待办清单也是历史，而打开它的人九成是来看
    待办的。要纯历史就带上 `status=resolved`。

    同一个 (账户, 种类, subject) 同时只会有一条 `open` —— 一个持续三天的余额问题
    是**一条**记录（`opened_at` 记着什么时候开始的），不是七十多条。
    """
    rows, total = await alert_service.list_alerts(
        session,
        status=alert_status,
        account_id=account_id,
        page=page,
        page_size=page_size,
    )
    return AlertListResponse(
        items=[AlertItem.model_validate(row) for row in rows],
        total=total,
    )


@router.post(
    "/alerts/sweep",
    response_model=SweepResponse,
    status_code=status.HTTP_200_OK,
    operation_id="sweepAlerts",
)
async def sweep_alerts(session: SessionDep, settings: SettingsDep) -> SweepResponse:
    """立刻巡检一遍，不等下一个整点。

    常态是 Celery beat 每小时自动跑（`db/broker.py` 的 `beat_schedule`），这个接口
    是给两种情况留的：刚改完阈值想马上看效果，以及 beat 没起来时手动补一次。

    **可以反复点**：对账是幂等的，同一件事只会有一条 `open`，重复巡检不会重复推送。
    """
    summary = await alert_service.sweep(session, settings)
    return SweepResponse.model_validate(summary)


@router.get(
    "/alerts/balances",
    response_model=BalanceAlertListResponse,
    operation_id="listBalanceAlerts",
)
async def list_balance_alerts(
    session: SessionDep,
    only_alerting: Annotated[
        bool,
        Query(description="只看已触发告警的；关掉就是所有录过余额的在投账户"),
    ] = True,
) -> BalanceAlertListResponse:
    """所有**在投**账户的余额现算清单，最紧急的排前面。

    和 `/alerts` 的区别：这里是**此刻重新算的**，不查 `alerts` 表。所以它能回答
    「虽然还没到阈值，但哪几个账户快了」，而告警表只有已经越线的。

    停投的账户（`is_active=false`）不看；从没录过余额的账户也不出现 —— 那是
    「不知道」，不是「没事」。

    **没有分页**：这是一张给人当天处理用的清单，长到需要翻页就说明该先去补余额
    数据，而不是往后翻。
    """
    found = await balance_service.alerts(session, only_alerting=only_alerting)
    return BalanceAlertListResponse(
        items=[to_runway_response(alert) for alert in found],
        total=len(found),
    )


@router.post(
    "/alerts/{alert_id}/diagnose",
    response_model=DiagnosisResponse,
    operation_id="diagnoseAlert",
    responses=responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
async def diagnose_alert(
    alert_id: int,
    session: SessionDep,
    settings: SettingsDep,
) -> DiagnosisResponse:
    """让模型解释这条告警：大概率是什么原因、接下来该核实什么。

    🔴 **按需调用，不自动。** 大部分告警一眼就知道原因（余额低了 → 充钱；花费涨了
    → 昨天调过预算），给每条都自动花一次钱，钱就花在了不需要解释的那些上面 ——
    而诊断的价值恰恰在难解释的少数（设计文档第七节）。

    **每点一次就是一次真实的模型调用**（会计入每日上限，超了返回 429）。没配 LLM
    时返回 503。模型没答上来时返回 **200 且 `diagnosis` 为 `null`** —— 那次调用
    已经烧了 token 并落了账，抛异常会把那条账一起回滚掉。

    输出里没有任何会被执行的东西：它给方向（「大概率是素材疲劳，建议换素材而不是
    降价」），不给指令。
    """
    outcome = await alert_service.diagnose(session, settings, alert_id=alert_id)
    return DiagnosisResponse(
        diagnosis=(
            DiagnosisItem.model_validate(outcome.diagnosis.model_dump())
            if outcome.diagnosis is not None
            else None
        ),
        llm_call_id=outcome.call.id,
    )
