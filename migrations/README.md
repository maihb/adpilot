# 迁移

`versions/` 是 schema 演进历史的真相源；**表定义的真相源是
[`src/adpilot/models/`](../src/adpilot/models/)**，这里只记录数据库怎么一步步
变成那个样子。

工作循环（命令见 [`CLAUDE.md`](../CLAUDE.md) 的「常用命令」）：

```text
改 models/ 下的声明
  → alembic revision --autogenerate -m "..."
  → 人看一遍生成的 versions/*.py      ← 唯一不能省的一步
  → alembic upgrade head
```

**为什么那一步省不掉**：autogenerate 认不出重命名，它会给你一对 drop + add，
跑下去数据就没了。这类盲区有几个，全部列在
[Schema 与迁移方案](../docs/design/2026-08-19-schema-migration.md)第四、五节。

删表 / 删列有门禁拦着（`tests/test_migration_safety.py`）：`upgrade()` 里出现
破坏性 DDL 就必须在文件里写一行 `# DESTRUCTIVE-OK: <理由>`，否则 CI 红。
