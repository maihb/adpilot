"""自动拉取这个领域的路由：授权凭据，以及拉取本身。

**要运营身份**（回调那条在 `api/oauth.py`，它必须公开）。

⚠️ 后三条的路径挂在 `/ad-accounts/{id}/` 底下，文件却在这里 —— 路径按**资源**
组织（它们操作的是某个账户），tag 按**领域**组织（它们属于自动拉取，不属于「客户
与账户」）。[BUSINESS.md](../../../docs/business/BUSINESS.md) 那张表按领域登记，
而 tag 是它的机器锚点，所以这里跟着领域走。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from adpilot.api.deps import CeleryDep, MongoDep, SessionDep, SettingsDep
from adpilot.api.errors import responses
from adpilot.schemas.ad_account import AdAccountResponse
from adpilot.schemas.fetch import (
    AttachCredentialRequest,
    AuthorizeUrlResponse,
    CredentialCreateRequest,
    CredentialRead,
    FetchRequest,
    FetchResponse,
    FetchStateRead,
)
from adpilot.services import ad_account as ad_account_service
from adpilot.services import credential as credential_service
from adpilot.services import fetch as fetch_service
from adpilot.services import task as task_service

router = APIRouter(tags=["credentials"])


@router.post(
    "/credentials/authorize-url",
    response_model=AuthorizeUrlResponse,
    operation_id="createAuthorizeUrl",
    responses=responses(status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def create_authorize_url(
    payload: CredentialCreateRequest,
    settings: SettingsDep,
) -> AuthorizeUrlResponse:
    """拿一个「去平台点同意」的地址。前端**直接跳转**过去，不要用 iframe。

    这一步不写任何东西 —— 凭据要等平台把人跳回
    `GET /api/oauth/tiktok/callback` 才落库。所以重复点这个按钮是安全的。

    没配 `TIKTOK_APP_ID` / `TIKTOK_APP_SECRET` / `OAUTH_REDIRECT_BASE_URL` 时
    返回 503：那是部署方的问题，不是点按钮的人的问题。
    """
    return AuthorizeUrlResponse(url=credential_service.authorize_url(settings, label=payload.label))


@router.get(
    "/credentials",
    response_model=list[CredentialRead],
    operation_id="listCredentials",
)
async def list_credentials(session: SessionDep) -> list[CredentialRead]:
    """全部平台授权，新的在前。**不分页** —— 凭据按授权来，个位数。

    🔴 出参里没有任何形式的 token，密文也没有。要换 token 只有一条路：重新走
    一次授权。
    """
    credentials = await credential_service.list_all(session)
    return [CredentialRead.model_validate(item) for item in credentials]


@router.post(
    "/credentials/{credential_id}/deactivate",
    response_model=CredentialRead,
    operation_id="deactivateCredential",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def deactivate_credential(credential_id: int, session: SessionDep) -> CredentialRead:
    """停用一个凭据：挂在它上面的账户从此不再被排期扫到。

    **不是删除。** 删掉之后那些历史快照的来源就成了无头案，而且挂着账户的凭据
    在数据库层面（RESTRICT）也删不掉。

    ⚠️ 停用**不影响手动触发**：先停用、再手动补一段历史，是一条正当路径。
    """
    credential = await credential_service.deactivate(session, credential_id)
    return CredentialRead.model_validate(credential)


@router.put(
    "/ad-accounts/{account_id}/credential",
    response_model=AdAccountResponse,
    operation_id="attachAccountCredential",
    responses=responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def attach_account_credential(
    account_id: int,
    request: AttachCredentialRequest,
    session: SessionDep,
) -> AdAccountResponse:
    """把账户挂到某个平台凭据上，`credential_id: null` 表示解绑。

    🔴 **挂上去就等于打开了自动拉取**，解绑就等于关掉 —— 没有第二个开关
    （`models/ad_account.py` 的 `credential_id` 讲了为什么不设）。

    平台对不上（TikTok 的账户挂到 Meta 的凭据上）返回 422：那种配置的症状是每次
    拉取都被平台拒绝，而那个报错完全不提「你挂错了」。
    """
    account = await ad_account_service.attach_credential(
        session,
        account_id,
        credential_id=request.credential_id,
    )
    return AdAccountResponse.model_validate(account)


@router.post(
    "/ad-accounts/{account_id}/fetch",
    response_model=FetchResponse,
    operation_id="fetchAccountData",
    responses=responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
async def fetch_account_data(
    account_id: int,
    request: FetchRequest,
    session: SessionDep,
    mongo: MongoDep,
    celery: CeleryDep,
    settings: SettingsDep,
) -> FetchResponse:
    """立刻从平台拉一次这个账户的数据。**排期任务走的是同一个服务函数。**

    三个场景要它：授权刚配好想当场验证通不通；平台回填了很久以前的数据要重拉一段
    历史；排期那次失败了，修好之后补一次。

    **同步拉，异步归一化**：拉取要当场把成败告诉人（token 失效、账户挂错凭据，
    都是要人立刻动手的事），而归一化重、且没人需要盯着看 —— 响应里的 `task_id`
    拿去 `GET /api/tasks/{task_id}` 看进度。`task_id` 为 `null` 表示队列连不上，
    **快照已经落好了**，补触发一次归一化即可，别重新拉（那只会多一条快照）。

    没挂凭据返回 422；平台那边出问题返回 502；`CREDENTIALS_SECRET` 缺失或换过
    返回 503。
    """
    levels = tuple(request.levels) if request.levels else fetch_service.DEFAULT_LEVELS
    summary = await fetch_service.fetch_account(
        session,
        mongo,
        settings,
        account_id=account_id,
        since=request.since,
        until=request.until,
        levels=levels,
    )
    # 手动拉成功也算成功：它同样让「这个账户的数据是新的」这句话成立，所以
    # `last_success_at` 要跟着走 —— 否则人补完数据，26 小时后照样收到一条
    # 「太久没拉到数」的告警。
    #
    # ⚠️ **失败时刻意不记**（那要另起一个事务，见 `tasks/fetch.py`）：手动触发
    # 的失败人当场就看到了，而排期那条路上的失败才是「没人看得见」的那种。
    await fetch_service.record_success(session, account_id=account_id)

    task_id = await task_service.enqueue_normalize(celery, account_id=account_id)
    return FetchResponse(
        account_id=summary.account_id,
        provider=summary.provider,
        since=summary.since,
        until=summary.until,
        levels=summary.levels,
        snapshots=summary.snapshots,
        rows=summary.rows,
        balance_captured=summary.balance_captured,
        task_id=task_id,
    )


@router.get(
    "/ad-accounts/{account_id}/fetch-state",
    response_model=FetchStateRead,
    operation_id="getAccountFetchState",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def get_account_fetch_state(account_id: int, session: SessionDep) -> FetchStateRead:
    """这个账户的自动拉取健康度：上次什么时候成的、现在连着失败几次了。

    ⚠️ **从来没拉过返回 404，不是一份全空的记录。** 两者含义完全不同：前者是
    「这个账户没接自动拉取」，后者会被读成「接了，只是还没跑」—— 而在一个靠
    「数据新不新」判断可信度的系统里，这个区别不能靠猜。
    """
    state = await fetch_service.state_for(session, account_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账户 {account_id} 还没有自动拉取记录",
        )
    return FetchStateRead.model_validate(state)
