"""投放操作记录的路由：登记进来、查回去。

**没有修改和删除。** 与余额快照同源（`api/balance.py`）：这张表是日报的证据链，
「上周为什么把预算提上去」是个会被回头追问的问题，而改掉或删掉就再也答不上来。
填错了就再登记一条说明 —— 那本身也是投放过程的一部分。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.action import ActionCreateRequest, ActionItem, ActionListResponse
from adpilot.services import action as action_service

router = APIRouter(tags=["actions"])


@router.post(
    "/ad-accounts/{account_id}/actions",
    response_model=ActionItem,
    status_code=status.HTTP_201_CREATED,
    operation_id="recordAction",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def record_action(
    account_id: int,
    payload: ActionCreateRequest,
    session: SessionDep,
) -> ActionItem:
    """登记一次投放调整。

    **手动登记是 MVP 的唯一入口，且这不是妥协**（设计文档第十节第 4 条）：接了
    Ads API 之后自动抓变更日志也只能补上「改了什么」，补不上 `reason` 那一段
    ——「为什么这么调」只存在于操作的人脑子里，当场不记，三天后就没了。而日报里
    真正值钱的正是那一段。

    `performed_at` 是操作**实际发生**的时刻（不是登记时刻），要带时区；落在未来
    的直接 422 —— 那样的记录永远不会出现在任何一期日报里。
    """
    action = await action_service.record(
        session,
        account_id=account_id,
        kind=payload.kind,
        summary=payload.summary,
        reason=payload.reason,
        performed_at=payload.performed_at,
        level=payload.level,
        object_id=payload.object_id,
        object_name=payload.object_name,
        operator=payload.operator,
    )
    return ActionItem.model_validate(action)


@router.get(
    "/ad-accounts/{account_id}/actions",
    response_model=ActionListResponse,
    operation_id="listActions",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_actions(
    account_id: int,
    session: SessionDep,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
) -> ActionListResponse:
    """列出某账户的操作记录，**最近做的在前**。

    排序按 `performed_at` 而不是登记时间：补登记的那几条录入更晚、描述的事更早，
    按登记时间排会读不出投放的先后。

    日报取的是另一个形态（某个自然日区间、按发生先后正序），走服务层的
    `list_in_window`，不经这个接口。
    """
    rows, total = await action_service.list_page(
        session,
        account_id=account_id,
        page=page,
        page_size=page_size,
    )
    return ActionListResponse(
        items=[ActionItem.model_validate(row) for row in rows],
        total=total,
    )
