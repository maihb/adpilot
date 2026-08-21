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


class InvalidDataError(DomainError):
    """送进来的外部数据本身不合法，比如一份解析不了的 CSV → 422。

    message 直接来自 `providers`，**必须带得上定位信息**（第几行、哪个字段、
    期望什么）。导入的人拿着一份几千行的文件，只说「解析失败」等于没说。
    """


class NotConfiguredError(DomainError):
    """服务端缺了这件事必需的配置 → 503。

    **是部署方的问题，不是调用方的问题**，所以不能回 4xx —— 那会让人对着一个
    永远失败的按钮反复试。message 可以直说缺哪个环境变量：那些名字在
    `.env.example` 里本来就写着，不是秘密（同 `api/errors.py` 里 AUTH_SECRET
    那条的处置）。
    """


class QuotaExceededError(DomainError):
    """撞上了自己设的用量上限 → 429。

    与「供应商限流」不是一回事：这是**本地闸门**，为的是防一个写错的循环在夜里
    把额度跑光。自托管意味着花的是使用者自己的钱，没有人替他兜底。
    """
