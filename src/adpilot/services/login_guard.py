"""登录失败计数与验证码答案的存取。

`auth/captcha.py` 负责出题和比对（纯计算），这一层负责**记住** —— 谁失败了几次、
哪张验证码的答案是什么。状态全在 Redis：它是过程状态不是业务事实，整个丢掉最坏的
后果是所有人的失败计数清零，而那正是重启一次就该发生的事。

整体设计见 [登录验证码](../../../docs/design/2026-08-25-login-captcha.md)。
"""

from __future__ import annotations

from redis.asyncio import Redis

from adpilot.auth import captcha

#: 连续失败几次之后开始要验证码。一次是手滑，两次是没记住，三次开始像在试。
CAPTCHA_AFTER_FAILURES = 2

#: 失败计数的存活时间。**每次失败都会重置它**，所以语义是「连续失败」——
#: 隔了一阵再来是新的开始，而不是把一整天的手滑攒起来。
FAILURE_TTL_SECONDS = 15 * 60

#: 验证码的存活时间。够抄完，又不至于让一张图在标签页里躺一下午。
CAPTCHA_TTL_SECONDS = 5 * 60

_FAILURE_KEY = "login:fail:{username}"
_CAPTCHA_KEY = "login:captcha:{captcha_id}"


def _failure_key(username: str) -> str:
    return _FAILURE_KEY.format(username=username)


def _captcha_key(captcha_id: str) -> str:
    return _CAPTCHA_KEY.format(captcha_id=captcha_id)


async def failure_count(redis: Redis, *, username: str) -> int:
    """这个账号连续失败了几次。Redis 不可用时返回 0。

    🔴 **连不上 Redis 时选择「不要验证码」而不是「一律要验证码」。** 后者看着更
    安全，实际是把一次基础设施抖动变成「谁也进不来」—— 而进不来的那个人正是要去
    修 Redis 的人。密码本身仍然拦着，这一层从来不是最后一道锁。
    """
    try:
        raw = await redis.get(_failure_key(username))
    except Exception:  # Redis 抖动不该让登录挂掉，理由见 docstring
        return 0
    return int(raw) if raw else 0


async def captcha_required(redis: Redis, *, username: str) -> bool:
    """现在这个账号登录要不要带验证码。"""
    return await failure_count(redis, username=username) >= CAPTCHA_AFTER_FAILURES


async def record_failure(redis: Redis, *, username: str) -> None:
    """记一次失败，并把 TTL 顶回去（于是计数的语义是「连续」）。"""
    key = _failure_key(username)
    try:
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, FAILURE_TTL_SECONDS)
        await pipe.execute()
    except Exception:  # 同 failure_count：宁可少记一次也不要登录挂掉
        return


async def clear_failures(redis: Redis, *, username: str) -> None:
    """登录成功，清零。"""
    try:
        await redis.delete(_failure_key(username))
    except Exception:  # 同上
        return


async def issue_captcha(redis: Redis) -> tuple[str, str]:
    """出一张新验证码，返回 `(captcha_id, svg)`。答案只存 Redis，不出这个函数。"""
    captcha_id = captcha.new_id()
    answer = captcha.new_answer()
    await redis.setex(_captcha_key(captcha_id), CAPTCHA_TTL_SECONDS, answer)
    return captcha_id, captcha.render_svg(answer)


async def consume_captcha(redis: Redis, *, captcha_id: str, answer: str) -> bool:
    """校验并**销毁**一张验证码。

    🔴 **不管答得对不对都删。** 留着的话，一张答对过的验证码可以被复用去驱动任意
    多次密码尝试 —— 那就等于没有验证码（设计文档「顺序就是安全性」一节）。

    Redis 不可用时返回 `False`：这里和 `failure_count` 的取舍方向相反，因为走到
    这一步说明**已经确定要验证码了**，此时验不了就不能放行。
    """
    if not captcha_id or not answer:
        return False
    try:
        expected = await redis.getdel(_captcha_key(captcha_id))
    except Exception:  # 已经确定要验证码，验不了就不放行
        return False
    if expected is None:
        return False
    # redis-py 的类型 stub 不知道我们建客户端时设了 `decode_responses=True`
    # （`db/redis.py`），于是它认为这里可能是 bytes。运行时不会，但顺手解一下比挂
    # 一句 type: ignore 强 —— 哪天有人换掉那个参数，这里仍然是对的。
    if isinstance(expected, bytes):
        expected = expected.decode("utf-8")
    return captcha.matches(expected, answer)
