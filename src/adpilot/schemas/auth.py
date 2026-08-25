"""认证的出入参。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# 密码长度上限**不是为了限制密码强度**，是为了挡住「拿一兆的字符串去打 argon2」
# —— 那个哈希故意很慢，长输入会让它更慢，而登录接口是不需要认证就能打的。
PASSWORD_MAX_LENGTH = 1024


class OperatorLoginRequest(BaseModel):
    """运营登录。账号从环境变量来，不建用户表（设计文档 2026-08-21 第五节）。

    验证码那两个字段**平时是空的** —— 只有连续失败到阈值之后才需要带，前端靠
    `GET /api/auth/captcha` 知道该不该显示那个框
    （[登录验证码](../../../docs/design/2026-08-25-login-captcha.md)）。
    """

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    captcha_id: str | None = Field(default=None, max_length=128)
    captcha_answer: str | None = Field(default=None, max_length=16)


class CaptchaResponse(BaseModel):
    """这个账号现在要不要验证码；要的话顺带把题给了。

    **合成一个接口而不是拆成「查状态」+「取图」**：拆开的话前端得连发两次，而
    第二次的结果会让第一次的答案过期（验证码用一次即删）—— 那种时序 bug 只在
    网络慢的时候出现。

    不需要验证码时 `captcha_id` 和 `image` 都是 `None`，**此时不会生成任何东西**，
    也就不会往 Redis 里塞一张没人会用的答案。
    """

    required: bool
    captcha_id: str | None = None
    #: `data:image/svg+xml;base64,...`，前端直接塞进 `<img src>`。
    image: str | None = None


class TokenResponse(BaseModel):
    """换出来的 token。

    `expires_at` 带出去，前端才能在**过期之前**主动续期，而不是等某个请求撞上
    401 再去补救 —— 后者的表现是用户点了一下没反应，然后被踹回登录页。
    """

    token: str
    expires_at: datetime


class InviteRedeemRequest(BaseModel):
    """客户端用邀请码换 token。

    长度上限挡的是「拿一兆的字符串当码来试」；真正的码是 32 个字符
    （`services/invite.py` 的 `CODE_BYTES`）。
    """

    code: str = Field(min_length=1, max_length=128)


class ClientTokenResponse(TokenResponse):
    """兑换出来的客户端 token，外加一个客户名。

    带上名字是为了让小程序在**第一屏**就能显示「XX 的投放看板」，而不必再发一次
    请求去问自己是谁 —— 扫码进来那一下是这条链路上唯一会被用户感知到的等待。
    """

    client_name: str
