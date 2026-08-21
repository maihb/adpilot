"""日报的路由：生成 → 修订 → 发布。

三个写操作按「能不能收回来」分级（[admin.md](../../../docs/business/admin.md)）：
生成可以重来（未发布的重新生成就是了）、修订可以再改、**发布收不回来** ——
客户手上那份不会自己更新，所以它是这三个里唯一需要二次确认的。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from adpilot.api.deps import SessionDep, SettingsDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.report import (
    ReportGenerateRequest,
    ReportItem,
    ReportListResponse,
    ReportReviseRequest,
)
from adpilot.services import report as report_service

router = APIRouter(tags=["reports"])


@router.post(
    "/ad-accounts/{account_id}/reports",
    response_model=ReportItem,
    status_code=status.HTTP_201_CREATED,
    operation_id="generateReport",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def generate_report(
    account_id: int,
    payload: ReportGenerateRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> ReportItem:
    """生成某一天的日报：先固定数字，再让模型写那一行人话。

    **数字在这一刻固定下来，此后不随平台回填变化**（glossary 的「日报快照」）。
    客户上周收到的日报今天再打开数字变了，比数字不够准更伤 —— 那是解释不清的。

    **模型挂了或没配 LLM 不影响这个接口成功**：日报照样生成，状态停在 `draft`、
    `llm_narrative` 为 `null`，人自己写那段话。数字部分是确定性的，不该被模型的
    可用性绑架。

    同一天重复调用会**重新生成**（覆盖数字和模型原文，并清掉人工修订 —— 数字变了，
    基于旧数字写的那段话未必还成立）。**已发布的那份不能重新生成**，返回 409。
    """
    report = await report_service.generate(
        session,
        settings,
        account_id=account_id,
        stat_date=payload.stat_date,
    )
    return ReportItem.model_validate(report)


@router.get(
    "/ad-accounts/{account_id}/reports",
    response_model=ReportListResponse,
    operation_id="listReports",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_reports(
    account_id: int,
    session: SessionDep,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
) -> ReportListResponse:
    """某账户的日报，最近那天的在前。含各个状态 —— 这是运营的工作台。"""
    rows, total = await report_service.list_page(
        session,
        account_id=account_id,
        page=page,
        page_size=page_size,
    )
    return ReportListResponse(
        items=[ReportItem.model_validate(row) for row in rows],
        total=total,
    )


@router.get(
    "/reports/{report_id}",
    response_model=ReportItem,
    operation_id="getReport",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def get_report(report_id: int, session: SessionDep) -> ReportItem:
    """一份日报的全部内容，**两版人话都给**。

    前端把 `llm_narrative` 预填进编辑框让人改，改完存进 `narrative` —— 两版都留着
    才回答得了「这句话是模型写的还是人改的」。
    """
    return ReportItem.model_validate(await report_service.get(session, report_id))


@router.patch(
    "/reports/{report_id}",
    response_model=ReportItem,
    operation_id="reviseReport",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def revise_report(
    report_id: int,
    payload: ReportReviseRequest,
    session: SessionDep,
) -> ReportItem:
    """存下人工修订后的那一版，并盖上「人看过了」的戳。

    🔴 **这一步不是走过场。** 模型可能在散文里写「成本上升了 40%」而实际是 24%，
    而没有任何机器判定拦得住这件事（正则抽数字会被「四成」绕过去）—— 人是唯一的
    防线，所以未经这一步的日报发不出去。

    **`llm_narrative` 不会被动**：模型原文永不修改。已发布的日报改不了（409）。
    """
    report = await report_service.revise(
        session,
        report_id=report_id,
        narrative=payload.narrative.model_dump(),
        reviewer=payload.reviewer,
    )
    return ReportItem.model_validate(report)


@router.post(
    "/reports/{report_id}/publish",
    response_model=ReportItem,
    operation_id="publishReport",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def publish_report(report_id: int, session: SessionDep) -> ReportItem:
    """发布。发布之后客户端就看得到了，**而这一步收不回来**。

    两条硬校验，都在服务层（不是 UI 提示）：**必须经人工修订**、**操作记录不能
    为空**。任一条不满足返回 409，消息里说清缺的是哪一件。

    发布之后这份日报不再变：既不能改、也不能重新生成。数字后来修正了，在新一期
    日报里说明 —— 客户手上那份截图不会自己更新，而「同一天的日报今天看和昨天看
    不一样」会让人怀疑全部数字。
    """
    return ReportItem.model_validate(await report_service.publish(session, report_id=report_id))
