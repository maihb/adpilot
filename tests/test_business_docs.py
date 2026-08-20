"""业务文档与 OpenAPI 的一致性。

守的是 [`docs/business/BUSINESS.md`](../docs/business/BUSINESS.md)「加一个领域时」
那三步里的后两步：**每个对外 tag 都要在索引表里登记，且文档链接不能是死的**。

以 `tag` 为锚点是因为它不可能被忘记填 —— 忘了接口就注册不出来。而「加了接口忘了
写业务文档」正是这套三层文档最容易腐烂的地方：代码一直在走，文档停在三个月前，
然后没人再敢照着它跳过源码，等于白写。

**机器只能验到这里。** 「文档写得对不对」验不了，仍然靠 review。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

BUSINESS_DIR = Path(__file__).resolve().parent.parent / "docs" / "business"
INDEX = BUSINESS_DIR / "BUSINESS.md"

# 索引表里 tag 那一列：第二个单元格中被反引号包住的那个词。没有对外接口的领域
# 填的是「—」，不带反引号，于是自然不会被这条正则捞到。
_TAG_CELL = re.compile(r"^\|[^|]+\|\s*`([^`]+)`\s*\|")

# markdown 链接里的 .md 目标，允许带锚点（`](api.md#命名)`）。
_MD_LINK = re.compile(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)")


def _index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


def _registered_tags() -> set[str]:
    return {
        match.group(1) for line in _index_text().splitlines() if (match := _TAG_CELL.match(line))
    }


def _openapi_tags(client: TestClient) -> set[str]:
    schema = client.get("/openapi.json").json()
    return {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }


def test_every_openapi_tag_is_registered(offline_client: TestClient) -> None:
    """接口上出现的 tag 必须在索引表里有一行。

    漏登记的后果不是「少一行文档」，而是这个领域的规则**没有任何地方写着** ——
    下一个人只能去读源码反推，而源码说得清「怎么做的」，说不清「为什么是这个
    口径」。
    """
    missing = _openapi_tags(offline_client) - _registered_tags()

    assert not missing, (
        f"这些 tag 没在 docs/business/BUSINESS.md 的索引表里登记：{sorted(missing)}。"
        "照 _template.md 加一篇 <tag>.md，并在表里补一行。"
    )


def test_registered_tags_are_actually_used(offline_client: TestClient) -> None:
    """反过来：表里登记了 tag，接口上却找不到它。

    通常意味着接口被删了或改了 tag 而文档没跟上 —— 留着一行说自己存在、实际
    已经没有对应接口的记录，比没有这行更误导人。
    """
    stale = _registered_tags() - _openapi_tags(offline_client)

    assert not stale, (
        f"这些 tag 在索引表里登记了，但没有任何接口在用：{sorted(stale)}。"
        "接口删了就把那一行也删掉，改名了就两边一起改。"
    )


def test_index_has_no_dead_links() -> None:
    """索引里的文档链接必须指向真实存在的文件。

    死链在 markdown 里是静默的 —— 渲染出来仍是一个能点的蓝字，点下去才 404。
    """
    dead = sorted(
        {
            target
            for target in _MD_LINK.findall(_index_text())
            if not (INDEX.parent / target).exists()
        }
    )

    assert not dead, f"docs/business/BUSINESS.md 里这些链接指向不存在的文件：{dead}"
