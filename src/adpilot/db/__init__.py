"""数据库客户端与连接生命周期。

PostgreSQL 放事务性业务事实，MongoDB 放 append-only 的平台原始快照，Redis 做
限流和缓存。两个库怎么分工，理由写在 `adpilot.db.mongo` 的模块 docstring 里。
"""
