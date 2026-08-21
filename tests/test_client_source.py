"""客户端源码的形状门禁。

这几条盯的都是「写错了不会报错、只会安静地显示成另一个意思」的事。前端**不做
E2E**（[客户端设计](../docs/design/2026-08-21-client-app.md) 第十一节），页面渲染
没有任何机器在验，所以只剩下盯代码形状这一条路 —— 和 `test_auth_token.py` 最后
那条扫 `verify` 源码是同一个套路。

**为什么写成 pytest 而不是 ESLint 规则**：规则本身只要一条正则，而为它引一整套
JS lint 工具链（eslint + vue 解析器 + ts 解析器）要装的东西比被测的规则重得多。
放在这里还有个额外好处 —— 它跟着已有的五道门禁一起跑，不必等 CI 的 frontend job
装完 Node。
"""

from __future__ import annotations

import re
from pathlib import Path

CLIENT_SRC = Path(__file__).resolve().parents[1] / "client" / "src"

#: 🔴 页面和 store 里禁止出现的数字转换。
#:
#: 后端的金额与比率序列化出来永远是 JSON 字符串，而 `Number(null) === 0` ——
#: `days_left` 为 null 的意思是「近期没花钱，算不出来」，一个 Number() 就把
#: 「不知道」变成了「还能撑 0 天」，于是每个暂停投放的账户都在客户屏幕上着火。
#:
#: 转换只许发生在 `utils/` 里，那里的每个函数都有单测钉着 null 的走向。
#: 注意 `Number.isFinite(` 不会命中：后面跟的是点号，不是左括号。
_NUMERIC_CAST = re.compile(r"\b(?:Number|parseFloat|parseInt)\s*\(")

#: 请求必须走唯一出口（`api/request.ts`）。散到页面里去的话，认证头、并发上限、
#: 401 续期就各写各的，而漏掉一个的症状是「某个页面偶尔要重新扫码」。
_RAW_REQUEST = re.compile(r"\buni\.request\s*\(")


def _sources(*subdirs: str) -> list[Path]:
    """收集要扫的源码。测试文件不算 —— 断言里出现 `Number(` 是正常的。"""
    files: list[Path] = []
    for subdir in subdirs:
        root = CLIENT_SRC / subdir
        files.extend(
            path
            for path in sorted(root.rglob("*"))
            if path.suffix in {".ts", ".vue"} and not path.name.endswith(".test.ts")
        )
    return files


#: 整行注释。跳过它们**不是妥协，是语义正确**：注释里的代码不执行，而「解释
#: 为什么不用 Number()」恰恰是这些文件里该写的话。行尾注释不在此列 —— 精确处理
#: 它要连字符串字面量一起解析，那个复杂度换不来什么。
_COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*|<!--)")


def _offenders(paths: list[Path], pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _COMMENT_LINE.match(line):
                continue
            if pattern.search(line):
                hits.append(f"{path.relative_to(CLIENT_SRC)}:{lineno}  {line.strip()}")
    return hits


def test_client_source_tree_is_where_we_think() -> None:
    """先确认扫描目标真的在。

    路径写错的话，下面几条会扫到一个空列表然后全绿 —— 一个恒为绿的门禁比没有
    门禁更糟，因为它让人以为这件事被管住了。
    """
    assert CLIENT_SRC.is_dir(), f"客户端源码目录不在：{CLIENT_SRC}"
    assert (CLIENT_SRC / "utils" / "decimal.ts").is_file()
    assert _sources("pages", "stores"), "扫不到任何页面源码，这几条门禁在空跑"


def test_pages_never_cast_numbers_themselves() -> None:
    """页面和 store 里不许做数字转换。"""
    hits = _offenders(_sources("pages", "stores"), _NUMERIC_CAST)
    assert not hits, (
        "页面里不许直接把字符串转成数字 —— Number(null) === 0，"
        "会把「算不出来」显示成「0」。请改用 utils/decimal.ts 里的格式化函数：\n" + "\n".join(hits)
    )


def test_only_the_request_module_talks_to_uni_request() -> None:
    """`uni.request` 只许出现在唯一出口里。"""
    everywhere = _sources("pages", "stores", "api", "utils")
    outside = [path for path in everywhere if path != CLIENT_SRC / "api" / "request.ts"]
    hits = _offenders(outside, _RAW_REQUEST)
    assert not hits, (
        "请求必须走 api/request.ts —— 认证头、并发上限、401 续期都在那里，"
        "绕过去的那条请求不会带票，也不会在票过期时续期：\n" + "\n".join(hits)
    )


def _template_lines(path: Path) -> list[tuple[int, str]]:
    """只取 `<template>` 块里的行。

    `<script>` 里的 docstring 满是 `**`（那是给读代码的人看的），而模板里的 `**`
    会**原样显示给客户**。两者在同一个文件里，所以得分开看。
    """
    lines: list[tuple[int, str]] = []
    inside = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("<template"):
            inside = True
            continue
        if stripped.startswith("</template"):
            inside = False
            continue
        if inside:
            lines.append((lineno, line))
    return lines


def test_templates_do_not_contain_markdown_emphasis() -> None:
    """模板里不许写 markdown 的 `**`。

    这条是踩出来的：写文档写惯了，往页面文案里带一个 `**强调**`，浏览器会老老实实
    把两个星号显示给客户。它不会报错，type-check 也拦不住 —— 只有人眼或这条能看见。
    要强调就用一个 `<text>` 加样式。
    """
    hits: list[str] = []
    for path in _sources("pages"):
        if path.suffix != ".vue":
            continue
        for lineno, line in _template_lines(path):
            # 模板里的 <!-- --> 注释同样不显示给客户，跳过。
            if _COMMENT_LINE.match(line):
                continue
            if "**" in line:
                hits.append(f"{path.relative_to(CLIENT_SRC)}:{lineno}  {line.strip()}")

    assert not hits, (
        "模板里的 ** 会原样显示成两个星号 —— 那是 markdown 语法，浏览器不认。"
        "要强调请用 <text> 加样式：\n" + "\n".join(hits)
    )


#: 客户端那份告警类型中文名映射。抓 `KIND_NAMES = { ... }` 里的键。
_KIND_BLOCK = re.compile(r"const KIND_NAMES:[^{]*\{(.*?)\n\}", re.DOTALL)
_KIND_KEY = re.compile(r"^\s*(\w+):", re.MULTILINE)


def test_alert_kind_names_match_the_backend_enum() -> None:
    """告警类型的中文名必须和后端的 `AlertKind` **双向对齐**。

    这份映射是后端枚举在前端的第二份拷贝，而页面对认不出的 kind 做了回落（显示
    原始标识）—— 那个回落是对的，但它也意味着**对不上时不会报错**，只会安静地把
    一个英文标识显示给客户。写那段代码时就把 `balance_low` 记成了
    `balance_runway`，跑起来才看见。

    两个方向都查，和 `test_business_docs.py` 拿 tag 当锚点是同一个套路：后端加了
    新类型而前端没给中文名要红；前端留着一个后端已经没有的键，同样要红 —— 后者
    通常意味着某个类型被改名了，而改名的那半边正是会安静出错的地方。
    """
    from adpilot.models.alert import AlertKind

    source = (CLIENT_SRC / "pages" / "alerts" / "alerts.vue").read_text(encoding="utf-8")
    block = _KIND_BLOCK.search(source)
    assert block is not None, "找不到 KIND_NAMES —— 是不是改名了？改了就把这条测试一起改"

    mapped = set(_KIND_KEY.findall(block.group(1)))
    backend = {kind.value for kind in AlertKind}

    assert mapped == backend, (
        f"客户端的告警类型中文名和后端 AlertKind 对不上。\n"
        f"  后端有而前端没给中文名：{sorted(backend - mapped) or '无'}\n"
        f"  前端有而后端已经没有：{sorted(mapped - backend) or '无'}"
    )


def test_generated_types_are_committed() -> None:
    """生成的类型必须**进 git**。

    不进的话，CI 里 `git diff --exit-code` 那道门禁就无从比起 —— 而它是「后端改了
    出参形状但没同步前端」唯一的拦截点。这里顺便验一眼它确实覆盖到了客户端接口，
    免得哪天生成命令指错了源。
    """
    schema = CLIENT_SRC / "api" / "generated" / "schema.ts"
    assert schema.is_file(), "client/src/api/generated/schema.ts 不在，跑一次 make openapi"

    text = schema.read_text(encoding="utf-8")
    for symbol in ("PortalMetricsResponse", "PortalRunwayResponse", "ClientTokenResponse"):
        assert symbol in text, f"生成的类型里没有 {symbol}，检查 make openapi 的导出源"
