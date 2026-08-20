"""认证的出入参。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# 密码长度上限**不是为了限制密码强度**，是为了挡住「拿一兆的字符串去打 argon2」
# —— 那个哈希故意很慢，长输入会让它更慢，而登录接口是不需要认证就能打的。
PASSWORD_MAX_LENGTH = 1024


class OperatorLoginRequest(BaseModel):
    """运营登录。账号从环境变量来，不建用户表（设计文档 2026-08-21 第五节）。"""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class TokenResponse(BaseModel):
    """换出来的 token。

    `expires_at` 带出去，前端才能在**过期之前**主动续期，而不是等某个请求撞上
    401 再去补救 —— 后者的表现是用户点了一下没反应，然后被踹回登录页。
    """

    token: str
    expires_at: datetime
