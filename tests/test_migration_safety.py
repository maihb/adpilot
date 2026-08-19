"""迁移里的破坏性 DDL 门禁。

[Schema 方案第六节](../docs/design/2026-08-19-schema-migration.md)定的两道门禁
之一，也是本方案最关键的补强：把「自动生成的 DDL 悄悄删列」从**靠人注意**变成
**机器拦人** —— 也就是 Atlas 用 migration lint 内置提供、而 Alembic 缺失的那个
能力。另一道（`alembic check`，管「改了 model 忘了生成迁移」）在 CI 里，因为
它要连真实数据库。

判定规则：`upgrade()` 里出现删表 / 删列，同一个文件里就必须有一行

    # DESTRUCTIVE-OK: <为什么这里删掉是对的>

**只扫 `upgrade()`，不扫 `downgrade()`。** 后者里的 `drop_table` 是回滚路径，
每一个建表迁移都有 —— 一起扫的话，门禁第一天就变成人人跳过的噪音。

检查逻辑写在这里而不是 `src/adpilot/`：它的对象是仓库自身的文件，不是运行时
代码，不该跟着 wheel 一起发出去。实现和对实现的测试放同一个文件，理由与
`test_bash_guard.py` 一样 —— 一道门禁自己也得有门禁。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"

# 会让数据不可逆地消失的操作。改名不在此列 —— 把 drop + add 手工并成一条
# `op.alter_column(..., new_column_name=...)` 正是 Schema 方案要求的做法，
# 拦它等于拦正确答案。
DESTRUCTIVE = re.compile(
    r"""
    op\.(?:drop_table|drop_column)\s*\(   # Alembic 的操作
    | \bDROP \s+ (?:TABLE|COLUMN)\b       # op.execute 里手写的 SQL
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 理由至少要有点内容。`# DESTRUCTIVE-OK: ok` 这种和没写一样。
CONFIRMATION = re.compile(r"#\s*DESTRUCTIVE-OK:\s*(\S.{9,})")

REASON = """迁移里有删表/删列，但没写确认注释。

  确认它删的不是还要的数据，然后在文件里加一行：
      # DESTRUCTIVE-OK: <为什么这里删掉是对的>

  如果这其实是一次**改名** —— autogenerate 会把改名读成 drop + add，
  数据会静默丢失。把那两条并成一条：
      op.alter_column("表名", "旧列名", new_column_name="新列名")
"""


def upgrade_source(source: str) -> str:
    """取出 `upgrade()` 的源码；没有这个函数就返回空串。"""
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return ast.get_source_segment(source, node) or ""
    return ""


def unconfirmed_destructive(source: str) -> bool:
    """`upgrade()` 里有破坏性 DDL、而全文没有确认注释。"""
    if not DESTRUCTIVE.search(upgrade_source(source)):
        return False
    return not CONFIRMATION.search(source)


# ---------------------------------------------------------------------------
# 判定本身的用例：误拦会卡住正常建表，误放会让一次删列悄悄过去
# ---------------------------------------------------------------------------

CREATE_TABLE = """
def upgrade() -> None:
    op.create_table("clients", sa.Column("id", sa.Integer(), nullable=False))


def downgrade() -> None:
    op.drop_table("clients")
"""

DROP_COLUMN_BARE = """
def upgrade() -> None:
    op.drop_column("campaigns", "spend")


def downgrade() -> None:
    op.add_column("campaigns", sa.Column("spend", sa.Numeric(20, 4)))
"""

DROP_COLUMN_CONFIRMED = """
def upgrade() -> None:
    # DESTRUCTIVE-OK: 这一列从没写入过，D3 建表时多加的，确认过全表为 NULL
    op.drop_column("campaigns", "legacy_flag")


def downgrade() -> None:
    op.add_column("campaigns", sa.Column("legacy_flag", sa.Boolean()))
"""

DROP_VIA_EXECUTE = """
def upgrade() -> None:
    op.execute("ALTER TABLE campaigns DROP COLUMN spend")
"""

RENAME_DONE_RIGHT = """
def upgrade() -> None:
    op.alter_column("campaigns", "spend", new_column_name="spend_usd")


def downgrade() -> None:
    op.alter_column("campaigns", "spend_usd", new_column_name="spend")
"""

TOO_THIN_A_REASON = """
def upgrade() -> None:
    # DESTRUCTIVE-OK: ok
    op.drop_column("campaigns", "spend")
"""


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(DROP_COLUMN_BARE, id="裸删列"),
        pytest.param(DROP_VIA_EXECUTE, id="手写 SQL 删列"),
        pytest.param(TOO_THIN_A_REASON, id="理由太短等于没写"),
    ],
)
def test_destructive_without_confirmation_is_flagged(source: str) -> None:
    assert unconfirmed_destructive(source)


@pytest.mark.parametrize(
    "source",
    [
        # downgrade 里的 drop_table 是回滚，扫它会把每个建表迁移都变成告警
        pytest.param(CREATE_TABLE, id="建表：drop 只在 downgrade 里"),
        pytest.param(DROP_COLUMN_CONFIRMED, id="删列且写了理由"),
        # 改名的正确写法不能被自己的门禁拦下
        pytest.param(RENAME_DONE_RIGHT, id="改名用 alter_column"),
    ],
)
def test_legitimate_migrations_pass(source: str) -> None:
    assert not unconfirmed_destructive(source)


# ---------------------------------------------------------------------------
# 真实迁移目录
# ---------------------------------------------------------------------------


def test_committed_migrations_are_confirmed() -> None:
    """仓库里已有的每一份迁移都得过这道门禁。"""
    offenders = [
        path.name
        for path in sorted(VERSIONS_DIR.glob("*.py"))
        if unconfirmed_destructive(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"{REASON}\n涉及：{', '.join(offenders)}"
