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


def _offenders(paths: list[Path], pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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
