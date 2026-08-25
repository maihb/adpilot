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


class UpstreamError(DomainError):
    """外部平台那边失败了（拉数据、换 token）→ 502。

    **与 `InvalidDataError` 分开**：后者的意思是「你送进来的东西不对」，这条的
    意思是「我们和平台之间出了问题」。混成一个的话，接口会对着一次平台限流回
    422，而 422 的言下之意是「改请求体再试」—— 那是一条把人引向错误方向的提示。

    🔴 **`retryable` 是给 `tasks/` 用的，不是给接口用的。** 同一个服务函数既被
    HTTP 请求调用、也被排期任务调用，而两者对「该不该重试」的处置完全不同：接口
    当场把错误告诉人，任务要决定是退避重试还是直接进死信队列。判定本身来自
    provider（那是平台知识，见 `providers/base.py` 的 `FetchError`），这一层只
    负责把它原样带过来。
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable
