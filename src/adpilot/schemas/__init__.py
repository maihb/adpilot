"""对外的 Pydantic 出入参。

**与 `models/` 分开是刻意的。** ORM 模型是表结构的真相源，会跟着数据库一起长出
内部字段（成本、供应商 ID）；这一层是**对外契约**，两个前端从 `/openapi.json`
生成代码和类型，改这里等于改下游的编译期约束。理由与「加一个接口」的四步见
[`docs/code-rules/api.md`](../../../docs/code-rules/api.md)。
"""
