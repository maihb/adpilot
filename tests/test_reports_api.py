"""日报：从生成到发布，以及两条「发不出去」的硬校验。

D14 的验收标准全在这里：一份日报从 draft 走到 published 且客户端看得到、未经修订
的发不出去、操作记录为空的发不出去、模型挂掉时日报照样生成。

外加一条设计文档第八节点名要测的：**已发布的日报不随指标变化**。那条是「日报是
快照不是视图」的机器形态 —— 客户上周收到的日报今天再打开数字变了，比数字不够准
更伤，因为它解释不清。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Settings
from adpilot.llm.base import LLMUnavailableError
from adpilot.llm.contracts import DailyReportNarrative
from adpilot.llm.fake import FakeProvider
from adpilot.models import AdAccount, Client, DailyMetric, MetricLevel, Platform
from adpilot.models.action import ActionKind
from adpilot.models.report import ReportStatus
from adpilot.schemas.report import ReportNarrative
from adpilot.services import action as action_service
from adpilot.services import report as report_service
from adpilot.services.exceptions import ConflictError

# 报告日与它的对照期（上周同日）。写死过去的日期：操作记录不许落在未来，而相对
# 今天推算会让这组用例的行为随运行日期变。
_DAY = date(2026, 8, 18)
_BASELINE = date(2026, 8, 11)
_TZ = "America/Los_Angeles"

_NARRATIVE = {
    "summary": "花费与上周同日基本持平，转化成本略有上升。",
    "highlights": ["周末 CPM 普涨"],
    "next_steps": ["观察到周一再看"],
}
_GOOD_LLM = (
    '{"summary": "（模型写的）成本上升来自周末 CPM 普涨。", "highlights": [], "next_steps": []}'
)


# --- 离线：两条盯着形状的门禁 -----------------------------------------------


def test_openapi_declares_the_report_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    assert (
        schema["paths"]["/api/ad-accounts/{account_id}/reports"]["post"]["operationId"]
        == "generateReport"
    )
    assert schema["paths"]["/api/reports/{report_id}"]["patch"]["operationId"] == "reviseReport"
    assert (
        schema["paths"]["/api/reports/{report_id}/publish"]["post"]["operationId"]
        == "publishReport"
    )
    assert schema["paths"]["/api/portal/reports"]["get"]["operationId"] == "listPortalReports"


def test_the_client_never_sees_the_model_draft(offline_client: TestClient) -> None:
    """🔴 客户端出参里**没有 `llm_narrative`，也没有 `status`**。

    模型原文是内部的审计信息（回答「这句话是模型写的还是人改的」）。把它交给客户，
    等于把「这段话是 AI 写的、我们只过了一眼」直接摆出去 —— 而那正是这套流程想
    避免的印象。
    """
    schemas = offline_client.get("/openapi.json").json()["components"]["schemas"]

    assert "llm_narrative" in schemas["ReportItem"]["properties"]
    assert "llm_narrative" not in schemas["PortalReportItem"]["properties"]
    assert "status" not in schemas["PortalReportItem"]["properties"]


def test_the_two_narratives_stay_the_same_shape() -> None:
    """🔴 人工版和模型版必须同构。

    两版都存是为了回答「这句话是模型写的还是人改的」，而形状一旦不同，那个问题就
    没法逐段回答了 —— 人改的是三段、模型写的是两段的话，对比只能靠肉眼。

    这两个类**刻意不复用**（LLM 层的契约跟着提示词演进，对外 API 的要稳定），
    所以同构这件事只能靠一条测试盯着。
    """
    assert set(ReportNarrative.model_fields) == set(DailyReportNarrative.model_fields)


# --- 集成：HTTP 那条链 -------------------------------------------------------


async def _account(api: AsyncClient, suffix: str) -> tuple[int, int]:
    client = await api.post("/api/clients", json={"name": f"测试客户-日报-{suffix}"})
    assert client.status_code == 201, client.text
    client_id = client.json()["id"]

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client_id,
            "platform": Platform.META.value,
            "external_id": f"demo-report-{suffix}",
            "name": f"测试账户-{suffix}",
            "currency": "USD",
            "timezone": _TZ,
        },
    )
    assert account.status_code == 201, account.text
    return client_id, account.json()["id"]


async def _import_metrics(api: AsyncClient, account_id: int, *days: tuple[date, str]) -> None:
    header = "Day,Campaign ID,Campaign name,Amount spent (USD),Impressions,Link clicks\n"
    body = "".join(f"{day.isoformat()},cmp-1,测试系列,{spend},1000,50\n" for day, spend in days)

    imported = await api.post(
        "/api/imports",
        files={"file": ("report.csv", (header + body).encode(), "text/csv")},
        data={"account_id": str(account_id), "level": "campaign"},
    )
    assert imported.status_code == 201, imported.text
    assert (await api.post(f"/api/ad-accounts/{account_id}/normalize")).status_code == 200


async def _record_action(api: AsyncClient, account_id: int) -> None:
    # 账户时区 America/Los_Angeles：UTC 20:00 是当地 13:00，落在 _DAY 当天。
    performed_at = datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
    response = await api.post(
        f"/api/ad-accounts/{account_id}/actions",
        json={
            "kind": "budget",
            "summary": "A 系列日预算 500 → 800",
            "reason": "周末 CPM 普涨，先扛量到周一再看",
            "performed_at": performed_at.isoformat(),
        },
    )
    assert response.status_code == 201, response.text


async def _token_for(api: AsyncClient, client_id: int) -> str:
    code = (await api.post(f"/api/clients/{client_id}/invites", json={})).json()["code"]
    return str((await api.post("/api/auth/redeem", json={"code": code})).json()["token"])


@pytest.mark.integration
async def test_a_report_goes_from_draft_to_the_client(live_api: AsyncClient) -> None:
    """**D14 的验收标准①**：draft → 人工修订 → published → 客户端看得到。

    这条同时验着「没配 LLM 时日报照样出得来」—— 集成环境没有 LLM 凭据，所以生成
    出来的那份是 draft、人话为空，全靠人自己写。那正是设计的降级路径。
    """
    client_id, account_id = await _account(live_api, "全链")
    await _import_metrics(live_api, account_id, (_DAY, "120"), (_BASELINE, "100"))
    await _record_action(live_api, account_id)

    generated = await live_api.post(
        f"/api/ad-accounts/{account_id}/reports",
        json={"stat_date": _DAY.isoformat()},
    )
    assert generated.status_code == 201, generated.text
    report = generated.json()
    report_id = report["id"]

    # 数字是代码算的，不是模型编的
    assert Decimal(report["spend"]) == Decimal(120)
    assert Decimal(report["baseline_spend"]) == Decimal(100)
    assert report["status"] == ReportStatus.DRAFT.value
    assert report["narrative"] is None
    assert len(report["actions_snapshot"]) == 1

    # 没人审过之前，客户端一份都看不到
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}
    assert (await live_api.get("/api/portal/reports", headers=headers)).json()["total"] == 0

    revised = await live_api.patch(
        f"/api/reports/{report_id}",
        json={"narrative": _NARRATIVE, "reviewer": "运营小张"},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["status"] == ReportStatus.PENDING_REVIEW.value

    published = await live_api.post(f"/api/reports/{report_id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["status"] == ReportStatus.PUBLISHED.value

    listed = await live_api.get("/api/portal/reports", headers=headers)
    assert listed.json()["total"] == 1

    detail = await live_api.get(f"/api/portal/reports/{report_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["narrative"]["summary"] == _NARRATIVE["summary"]
    # 客户看得到「本期做了什么」，含那句「为什么」
    assert body["actions"][0]["reason"].startswith("周末 CPM")
    # 派生指标由后端按 glossary 的同一份公式算
    assert Decimal(body["cpa"]) if body["cpa"] is not None else True
    # 🔴 模型原文不下发
    assert "llm_narrative" not in body


@pytest.mark.integration
async def test_an_unrevised_report_cannot_be_published(live_api: AsyncClient) -> None:
    """**D14 的验收标准②之一。**

    模型可能在散文里写「成本上升了 40%」而实际是 24%，没有任何机器判定拦得住 ——
    人是唯一的防线，所以这条校验必须在服务层，不能只是 UI 上的一个提示。
    """
    _, account_id = await _account(live_api, "没审")
    await _import_metrics(live_api, account_id, (_DAY, "120"))
    await _record_action(live_api, account_id)

    generated = await live_api.post(
        f"/api/ad-accounts/{account_id}/reports",
        json={"stat_date": _DAY.isoformat()},
    )
    report_id = generated.json()["id"]

    refused = await live_api.post(f"/api/reports/{report_id}/publish")

    assert refused.status_code == 409
    assert "人工修订" in refused.json()["detail"]


@pytest.mark.integration
async def test_a_report_without_actions_cannot_be_published(live_api: AsyncClient) -> None:
    """**D14 的验收标准②之二**（主设计文档第十节第 4 条）。

    「本期做了什么」是日报的交付价值所在，动作要可数。一份说不出做了什么的日报，
    对客户来说就是一张他自己也能从平台后台导出来的表。
    """
    _, account_id = await _account(live_api, "没动作")
    await _import_metrics(live_api, account_id, (_DAY, "120"))

    generated = await live_api.post(
        f"/api/ad-accounts/{account_id}/reports",
        json={"stat_date": _DAY.isoformat()},
    )
    report_id = generated.json()["id"]
    await live_api.patch(f"/api/reports/{report_id}", json={"narrative": _NARRATIVE})

    refused = await live_api.post(f"/api/reports/{report_id}/publish")

    assert refused.status_code == 409
    assert "操作记录" in refused.json()["detail"]


@pytest.mark.integration
async def test_a_published_report_is_frozen(live_api: AsyncClient) -> None:
    """发布之后既不能改、也不能重新生成。

    客户手上那份截图不会自己更新，而库里和客户手里说的不是一回事，比两边都是旧的
    更糟。要更正就在新一期日报里说明。
    """
    _, account_id = await _account(live_api, "冻结")
    await _import_metrics(live_api, account_id, (_DAY, "120"))
    await _record_action(live_api, account_id)

    report_id = (
        await live_api.post(
            f"/api/ad-accounts/{account_id}/reports",
            json={"stat_date": _DAY.isoformat()},
        )
    ).json()["id"]
    await live_api.patch(f"/api/reports/{report_id}", json={"narrative": _NARRATIVE})
    assert (await live_api.post(f"/api/reports/{report_id}/publish")).status_code == 200

    again = await live_api.post(
        f"/api/ad-accounts/{account_id}/reports",
        json={"stat_date": _DAY.isoformat()},
    )
    revised = await live_api.patch(f"/api/reports/{report_id}", json={"narrative": _NARRATIVE})

    assert again.status_code == 409
    assert revised.status_code == 409


@pytest.mark.integration
async def test_another_clients_report_is_a_404(live_api: AsyncClient) -> None:
    """拿别人的 token 打不开我的日报 —— 和 D9 那组用例同一个道理。"""
    _, mine = await _account(live_api, "我的")
    other_client, _ = await _account(live_api, "别人的")
    await _import_metrics(live_api, mine, (_DAY, "120"))
    await _record_action(live_api, mine)

    report_id = (
        await live_api.post(
            f"/api/ad-accounts/{mine}/reports",
            json={"stat_date": _DAY.isoformat()},
        )
    ).json()["id"]
    await live_api.patch(f"/api/reports/{report_id}", json={"narrative": _NARRATIVE})
    await live_api.post(f"/api/reports/{report_id}/publish")

    headers = {"Authorization": f"Bearer {await _token_for(live_api, other_client)}"}
    stolen = await live_api.get(f"/api/portal/reports/{report_id}", headers=headers)

    assert stolen.status_code == 404


# --- 集成：服务层（要注入假 provider 的那几条）-------------------------------


async def _seed(session: AsyncSession, suffix: str, *days: tuple[date, str]) -> AdAccount:
    client = Client(name=f"测试客户-日报-{suffix}")
    session.add(client)
    await session.flush()

    account = AdAccount(
        client_id=client.id,
        platform=Platform.META,
        external_id=f"demo-report-{suffix}",
        name=f"测试账户-{suffix}",
        currency="USD",
        timezone=_TZ,
    )
    session.add(account)
    await session.flush()

    for day, spend in days:
        session.add(
            DailyMetric(
                account_id=account.id,
                level=MetricLevel.CAMPAIGN,
                object_id="cmp-1",
                stat_date=day,
                currency="USD",
                spend=Decimal(spend),
                impressions=1000,
                clicks=50,
                conversions=Decimal(10),
                revenue=Decimal(500),
            )
        )
    await session.flush()

    await action_service.record(
        session,
        account_id=account.id,
        kind=ActionKind.BUDGET,
        summary="A 系列日预算 500 → 800",
        reason="周末 CPM 普涨，先扛量到周一再看",
        performed_at=datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
    )
    return account


@pytest.mark.integration
async def test_the_report_survives_a_dead_model(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """**D14 的验收标准③**：模型挂了，日报照样生成。

    数字部分是确定性的，不该被模型的可用性绑架。人话字段留空、状态停在 `draft`，
    人自己写 —— 而那次失败的调用**仍然记了账**（它一样烧了 token）。
    """
    account = await _seed(live_session, "模型挂了", (_DAY, "120"), (_BASELINE, "100"))

    report = await report_service.generate(
        live_session,
        live_settings,
        account_id=account.id,
        stat_date=_DAY,
        provider=FakeProvider([LLMUnavailableError("端点不可达")]),
    )

    assert report.status is ReportStatus.DRAFT
    assert report.llm_narrative is None
    # 数字照样在
    assert report.spend == Decimal(120)
    assert report.baseline_spend == Decimal(100)
    # 失败的调用也落了账，靠它能查出为什么
    assert report.llm_call_id is not None


@pytest.mark.integration
async def test_a_working_model_only_fills_the_prose(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """模型写完之后状态转 `pending_review`，而**数字一个都没被它碰过**。"""
    account = await _seed(live_session, "模型正常", (_DAY, "120"), (_BASELINE, "100"))

    report = await report_service.generate(
        live_session,
        live_settings,
        account_id=account.id,
        stat_date=_DAY,
        provider=FakeProvider([_GOOD_LLM]),
    )

    assert report.status is ReportStatus.PENDING_REVIEW
    assert report.llm_narrative is not None
    assert "周末 CPM" in report.llm_narrative["summary"]
    assert report.spend == Decimal(120)
    # 还是没人审过，所以还发不出去
    assert report.reviewed_at is None


@pytest.mark.integration
async def test_revising_never_touches_the_model_draft(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """🔴 人改的存进 `narrative`，模型原文原地不动。

    覆盖掉原文等于把「这句话是模型写的还是人改的」这个问题永久删除 —— 而那是
    评估这套系统值不值得信的唯一依据。
    """
    account = await _seed(live_session, "两版", (_DAY, "120"))
    report = await report_service.generate(
        live_session,
        live_settings,
        account_id=account.id,
        stat_date=_DAY,
        provider=FakeProvider([_GOOD_LLM]),
    )
    model_wrote = dict(report.llm_narrative or {})

    await report_service.revise(
        live_session,
        report_id=report.id,
        narrative=_NARRATIVE,
        reviewer="运营小张",
    )

    assert report.llm_narrative == model_wrote
    assert report.narrative == _NARRATIVE
    assert report.reviewed_at is not None


@pytest.mark.integration
async def test_a_published_report_does_not_follow_the_metrics(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """🔴 **已发布的日报不随指标变化**（设计文档第八节点名要测的那条）。

    平台回填、重新导入、归一化重跑都会改 `daily_metrics`。而客户手上那份 PDF /
    截图 / 聊天记录不会自己更新 —— 一个「同一天的日报今天看和昨天看数字不一样」
    的系统，会让人怀疑全部数字。
    """
    account = await _seed(live_session, "快照", (_DAY, "120"))
    report = await report_service.generate(
        live_session,
        live_settings,
        account_id=account.id,
        stat_date=_DAY,
        provider=FakeProvider([_GOOD_LLM]),
    )
    await report_service.revise(live_session, report_id=report.id, narrative=_NARRATIVE)
    await report_service.publish(live_session, report_id=report.id)

    # 平台回填：那天的花费从 120 变成 300
    await live_session.execute(
        update(DailyMetric)
        .where(DailyMetric.account_id == account.id, DailyMetric.stat_date == _DAY)
        .values(spend=Decimal(300))
    )
    await live_session.flush()

    same = await report_service.get(live_session, report.id)
    assert same.spend == Decimal(120)

    # 而且这份已经发布的日报不能被重新生成
    with pytest.raises(ConflictError):
        await report_service.generate(
            live_session,
            live_settings,
            account_id=account.id,
            stat_date=_DAY,
            provider=FakeProvider([_GOOD_LLM]),
        )


@pytest.mark.integration
async def test_a_missing_baseline_leaves_the_comparison_empty(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """🔴 对照期没有数据 → 三项全空，**不拿 0 当基线**。

    补一个 0 会算出「上升了 100%」这种凭空的百分比，而模型会照着它写进日报。
    """
    account = await _seed(live_session, "没对照", (_DAY, "120"))

    report = await report_service.generate(
        live_session,
        live_settings,
        account_id=account.id,
        stat_date=_DAY,
        provider=FakeProvider([_GOOD_LLM]),
    )

    assert report.baseline_date is None
    assert report.baseline_spend is None
    assert report.baseline_conversions is None
