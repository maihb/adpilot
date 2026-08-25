"""登录验证码的纯计算部分：出题、渲染、比对。

**这个模块不碰任何 IO。** `auth/` 在分层图里被压到够不着 `db`
（[architecture.md](../../../docs/code-rules/architecture.md)），所以「答案存哪、
失败次数记在哪」全在 `services/login_guard.py`。这里只回答三件事：题面长什么样、
画成什么、答得对不对。

好处是它能用一张参数化表测完，不必起 Redis。

整体设计见 [登录验证码](../../../docs/design/2026-08-25-login-captcha.md)。
"""

from __future__ import annotations

import hmac
import secrets

#: 出题用的字符集。**刻意剔掉了 0/O/D、1/I/L、2/Z、5/S、8/B** —— 这些字符在扭曲
#: 之后人眼分不开，而分不开的代价不是「安全性高一点」，是合法用户抄三遍才对。
#: 剩下 21 个字符，4 位约 19.4 bit，对「挡住无脑脚本」这个目标绰绰有余。
ALPHABET = "34679ACEFGHJKMNPQRTUVWXY"

#: 4 位。再长不会更安全（爆破成本已经远高于 argon2 那几十毫秒），只会更难抄。
LENGTH = 4

#: 验证码 id 的字节数。它只是个查找键、不是秘密，但仍然要不可猜 —— 可枚举的话，
#: 攻击者能拿别人刚生成还没用的那张去配自己的密码尝试。
ID_BYTES = 16

_WIDTH = 120
_HEIGHT = 44


def new_id() -> str:
    """生成一个不可猜的验证码 id。"""
    return secrets.token_urlsafe(ID_BYTES)


def new_answer() -> str:
    """出一道题。返回的是**标准答案**（大写），存进 Redis 的就是它。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def matches(expected: str, supplied: str) -> bool:
    """比对答案。**大小写不敏感**，两侧空白忽略。

    用 `compare_digest` 而不是 `==`：这里的时序泄露实际危害极小（答案只活一次、
    5 分钟），但写成常量时间比对的成本是零，而「哪些地方该用它」一旦开始靠个案
    判断，迟早会漏在真正要紧的那处。`auth/token.py` 里那处就是要紧的。
    """
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.strip().upper(), supplied.strip().upper())


def render_svg(answer: str) -> str:
    """把答案画成一张 SVG。

    **零依赖是这个函数存在的全部理由** —— 引 Pillow 只为画四个字符不值当
    （设计文档「不做什么」一节）。SVG 是纯字符串，浏览器原生渲染，前端拿
    `data:image/svg+xml;base64,...` 直接塞进 `<img>` 就行。

    ⚠️ **别把它当成防 OCR 的手段。** 扭曲和干扰线挡的是「拿脚本无脑打一万次」，
    不是有人专门写了个识别器。真到那一步该换的是部署形态，不是把字符扭更狠。

    ⚠️ **答案里的字符全部来自 `ALPHABET`（大写字母和数字），所以这里不做 XML
    转义。** 调用方要是塞了别的东西进来，那是调用方的 bug —— 但为了让这条约束
    不只活在注释里，函数开头直接断言。
    """
    if not all(char in ALPHABET for char in answer):
        raise ValueError("验证码只能由 ALPHABET 里的字符组成")

    # 背景条纹 + 干扰线：让整幅图没有大块纯色，简单的二值化分割就切不干净。
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
        f'viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="验证码">',
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="#f4f6f8"/>',
    ]

    for _ in range(3):
        x1, y1 = secrets.randbelow(_WIDTH), secrets.randbelow(_HEIGHT)
        x2, y2 = secrets.randbelow(_WIDTH), secrets.randbelow(_HEIGHT)
        shade = 150 + secrets.randbelow(60)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="rgb({shade},{shade},{shade})" stroke-width="1"/>'
        )

    step = _WIDTH // (len(answer) + 1)
    for index, char in enumerate(answer):
        x = step * (index + 1)
        y = _HEIGHT // 2 + 8 + secrets.randbelow(7) - 3
        angle = secrets.randbelow(31) - 15
        hue = secrets.randbelow(60) + 200  # 蓝紫区间，避开背景的灰
        parts.append(
            f'<text x="{x}" y="{y}" font-family="Menlo,Consolas,monospace" '
            f'font-size="26" font-weight="600" fill="hsl({hue},45%,35%)" '
            f'text-anchor="middle" transform="rotate({angle} {x} {y})">{char}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
