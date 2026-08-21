"""LLM 调用日志与成本。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.llm.base import CallStatus
from adpilot.llm.prompts import Purpose
from adpilot.models.mixins import TimestampMixin
from adpilot.models.types import StrEnumType

# ⚠️ **刻意不用 `daily_metric.MONEY`（`numeric(20,4)`）那个通用精度。** LLM 的单价
# 是「每百万 token」，于是单次调用的成本天然落在小数点后六到八位：一次日报大约
# 0.0002，而 4 位小数会把它舍成 0.0002 还好，0.00004 那种直接舍成 0。成本只有
# **累计起来**才有意义，而每行都舍一次的累计值会系统性偏低 —— 那正是这张表要
# 回答的第一个问题（这个月花了多少）。
#
# 这不违反「金额一律 numeric」那条规矩，它禁的是浮点，不是更高的精度。
TOKEN_COST = Numeric(20, 8)


class LLMCall(Base, TimestampMixin):
    """一次 LLM 调用的记账与留痕。**成功和失败都记。**

    这张表至少要能回答三个问题（设计文档第六节）：这个月花了多少、三个月后那份
    日报是什么口径生成的、那次为什么失败。三个问题各自钉住了一组列，下面逐条说
    为什么是这么设计的。

    ### 🔴 `prompt_version` 是最容易漏、也最难补的一列

    提示词改一次，日报的口径就变了一次。没有版本号的话，三个月后没有任何办法把
    「这份日报」和「当时那版提示词」对上 —— 而那是评估这套系统值不值得信的唯一
    依据。理由与「平台字段会漂移、上个月的口径三个月后要查得到」同源。

    提示词本身**不存库**：它是代码里的常量（`llm/prompts.py`），拿
    `(purpose, prompt_version)` 去代码历史里找回原文。存一份副本的话，「线上跑的
    是哪一版」立刻有了两个真相。

    ### 🔴 不记请求正文，也不记响应正文

    提示词里有客户的花费数字。这张表是给人查成本和排障用的，谁都可能把它导出来
    贴到别处 —— 而导出的那一刻，客户数据就跟着走了。失败原因只记**异常类名**
    （`error_type`），不记消息：驱动和供应商的报错文本里会带着 URL、甚至原样回显
    请求内容。

    ### token 数为什么可空

    **不是所有兼容端点都回 `usage`**（本地 vLLM、部分代理层会省掉）。缺了记 NULL
    而不是 0：0 的意思是「没花」，实情是「不知道花了多少」。混起来会让月度统计
    悄悄少一截，而那种少法看起来完全正常。

    `estimated_cost` 同理 —— 没配单价（`LLM_INPUT_COST_PER_MTOK` 等）时它是 NULL，
    表示「不估」，不是「免费」。
    """

    __tablename__ = "llm_calls"

    __table_args__ = (
        # 两个查询形态都以时间打头：「这个月花了多少」按区间扫，「今天还剩多少
        # 次额度」按当天零点起算。
        Index(None, "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)

    # 这次调用是为哪个账户做的。**可空**，且删账户时置空而不是级联删除：这行是
    # 财务留痕，账户没了钱也已经花掉了，不该跟着消失。有了它才答得上「这个客户
    # 的 LLM 成本是多少」——那是「这个月花了多少」的自然下一个问题，而事后补
    # 补不回来。
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("ad_accounts.id", ondelete="SET NULL"),
        index=True,
    )

    #: 拿它做什么。取值与 `llm/prompts.py` 里那份提示词的名字是**同一个枚举**，
    #: 所以「这条记录是哪份提示词生成的」永远对得上。
    purpose: Mapped[Purpose] = mapped_column(StrEnumType(Purpose, 32))

    #: 适配器的名字（`openai_compat` / `fake`），不是供应商的品牌名 —— 同一个适配器
    #: 接得上十几家，品牌名要从 `LLM_BASE_URL` 看。
    provider: Mapped[str] = mapped_column(String(32))

    #: 供应商**实际用的**模型名（网关会做别名映射）。单价是按它算的。
    model: Mapped[str] = mapped_column(String(64))

    #: 🔴 见类 docstring。格式 `v<n>`。
    prompt_version: Mapped[str] = mapped_column(String(16))

    status: Mapped[CallStatus] = mapped_column(StrEnumType(CallStatus, 16))

    #: 实际发出去几次请求。>1 就意味着有过一次不合格的输出，多出来的 token 照样计费。
    attempts: Mapped[int] = mapped_column(Integer())

    prompt_tokens: Mapped[int | None] = mapped_column(Integer())
    completion_tokens: Mapped[int | None] = mapped_column(Integer())

    #: 预估成本。NULL = 没配单价（「不知道」，不是「免费」）。精度比别处的金额
    #: 更细，理由见文件顶部的 `TOKEN_COST`。
    estimated_cost: Mapped[Decimal | None] = mapped_column(TOKEN_COST)

    #: 失败时的**异常类名**，成功时为空。见类 docstring 为什么不记消息。
    error_type: Mapped[str | None] = mapped_column(String(64))

    #: 这次调用（含重试）花了多久。慢到几十秒时日报任务会跟着卡住，而那件事只有
    #: 记了时长才看得见。
    duration_ms: Mapped[int] = mapped_column(Integer())
