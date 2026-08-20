"""领域异常。

`services/` 只抛这一族，`api/errors.py` 负责翻成 HTTP 状态码 —— 对应关系集中写
在那个文件里，加一个异常类就要去那边补一行，否则它会落到 500。

🔴 **`message` 会原样回给客户端**，所以只写业务事实（哪个资源、哪个键冲突），
绝不要把驱动异常的文本拼进去 —— 那里面可能带着 DSN 或主机名。
"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则层面的错误。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    """路径上指名的资源不存在 → 404。"""


class ConflictError(DomainError):
    """与已有数据冲突，通常是撞了唯一约束 → 409。"""


class ReferenceNotFoundError(DomainError):
    """入参里引用了一个不存在的对象（如建账户时给的 `client_id`）→ 422。

    **与 `NotFoundError` 分开是因为客户端要采取的行动不同**：404 的意思是「换
    个 URL」，422 的意思是「改请求体」。混成一个的话，前端只能靠读文案猜。
    """
