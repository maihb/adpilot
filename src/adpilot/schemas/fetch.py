"""自动拉取的出入参：凭据、拉取结果、拉取状态。"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from adpilot.models.ad_account import Platform
from adpilot.models.daily_metric import MetricLevel

LABEL_MAX_LENGTH = 128


class CredentialRead(BaseModel):
    """一条平台授权。

    🔴 **没有 token 字段，一个都没有** —— 密文也不给。存进库的密文本身没有直接
    危害，但把它下发给前端就意味着它会经过日志、浏览器缓存和任何一个中间层，
    而那些地方的留存策略没有人在管。要换 token 只有一条路：重新走一次授权。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: Platform

    #: 用哪个适配器去拉。它会跟着快照落进 `raw_reports.provider`，是历史数据的
    #: 来源标记。
    provider: str

    label: str

    #: 这次授权覆盖到的平台账户 ID。挂账户时照着它填，省得回平台后台去抄。
    #: **不是权限判据** —— 真正的判据是每次调用时平台自己的回答。
    external_account_ids: list[str]

    #: 什么时候过期。**TikTok 的长期 token 恒为 `null`**，这一项是给 Meta 留的
    #: （那边有 60 天大限）。
    expires_at: datetime | None

    is_active: bool
    created_at: datetime


class AuthorizeUrlResponse(BaseModel):
    """把人送去平台点「同意」的那个地址。

    前端**直接跳转**过去，不要用 iframe 或弹窗打开：平台的授权页会拒绝被嵌套，
    而那个失败在控制台里表现为一条 X-Frame-Options 报错，跟授权流程毫无关系。
    """

    url: str


class FetchRequest(BaseModel):
    """手动触发一次拉取。三项都可以不填。"""

    #: 起止日期，**两端都含**。都不填就用滚动窗口（配置 `FETCH_WINDOW_DAYS`，
    #: 默认最近 3 天）—— 那是排期任务走的那条路。填了就是补历史：平台回填了很久
    #: 以前的数据、或者某几天当初拉失败了。
    since: date | None = None
    until: date | None = None

    #: 拉哪几层。不填就是账户级 + 系列级（`services/fetch.py` 的 `DEFAULT_LEVELS`
    #: 讲了为什么广告级不在默认里）。
    levels: list[MetricLevel] | None = None


class FetchResponse(BaseModel):
    """一次拉取的结果摘要。**不回数据本身** —— 那可能是几千行。"""

    model_config = ConfigDict(from_attributes=True)

    account_id: int
    provider: str

    #: 实际拉的区间，两端都含。**回显它是有用的**：不填日期时这个区间由账户时区
    #: 和日切延迟算出来，而「为什么没拉到昨天的数」十次里有九次的答案在这两个值上。
    since: date
    until: date

    levels: list[MetricLevel]
    snapshots: int
    rows: int

    #: 余额拉到没有。**`false` 不是失败** —— 有些账户类型平台不给余额，那时日
    #: 指标照样是好的。
    balance_captured: bool

    #: 排上队的归一化任务，拿它去 `GET /api/tasks/{task_id}` 看进度。
    #: **`null` 表示没排上队**（队列连不上）—— 快照已经落好了，补触发一次归一化
    #: 即可，别重新拉一遍（那只会多一条快照）。
    task_id: str | None


class FetchStateRead(BaseModel):
    """某个账户的自动拉取健康度。

    ⚠️ **接口在「从来没拉过」时返回 404 而不是一份全空的记录**：两者的含义完全
    不同 —— 前者是「这个账户没接自动拉取」，后者会被读成「接了，只是还没跑」。
    """

    model_config = ConfigDict(from_attributes=True)

    account_id: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int


class AttachCredentialRequest(BaseModel):
    """把账户挂到某个凭据上，`null` 表示解绑（从此不再自动拉取）。"""

    credential_id: int | None = None


class CredentialCreateRequest(BaseModel):
    """发起授权时要填的东西。目前只有一个名字。"""

    #: 人给这次授权起的名字（「nail 的 BC 授权」）。后台列表全靠它认 —— token
    #: 本身不可读，而 `platform` 只有两三种值。
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
