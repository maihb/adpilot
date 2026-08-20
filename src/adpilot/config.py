"""应用配置，全部从环境变量读取。

所有可调项只在这里出现一次。这个模块守两条规矩：

1. **凭据没有可用的默认值。** 缺了就在启动时大声失败，而不是悄悄退回某个
   「本地能跑」的值，然后一路跟到生产环境。
2. **凭据一律用 `SecretStr`。** 它们不会因为一次日志、一次异常栈或一次
   `repr()` 意外泄出来；要读必须显式调 `.get_secret_value()`，review 时
   一 grep 就能找全。
3. **连接串一律由零件拼，不收整条 URI。** 四个外部系统都是 `*_HOST` / `*_PORT` /
   账号密码进来，DSN 由下面的 property 拼出去。收整条 URI 的代价是同一个事实
   有两处真相：`MONGO_PORT` 和 `MONGO_URI` 里的端口迟早会不一致，而症状是
   「compose 里跑得好好的，本机连到别的服务上去了」—— 一个不会报错、只会给出
   奇怪结果的失败。

⚠️ 拼出来的那几个连接串**里面带着明文密码**（驱动只认这个形态）。它们只喂给
驱动，不要记进日志、不要放进异常消息 —— `SecretStr` 到这一步就保护不了了。
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """部署环境。生产环境不能放松的护栏都以这个值为判据。"""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


def _credentials(user: str, password: SecretStr) -> str:
    """拼出连接串里的 `user:password@` 段，两边都做 percent-encode。

    🔴 **编码不是可有可无的。** README 让人用 `openssl rand -base64 24` 生成
    密码，而 base64 的字符集里就有 `+` 和 `/` —— 一个 `/` 会把 DSN 从那里截断，
    于是驱动去连一个根本不存在的主机，报错跟「密码里有个斜杠」八竿子打不着。
    用户名同样编码：`@` 出现在用户名里是一样的效果。
    """
    return f"{quote(user, safe='')}:{quote(password.get_secret_value(), safe='')}@"


class Settings(BaseSettings):
    """运行期配置，来源是环境变量或本地 .env。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEV
    debug: bool = False

    app_name: str = "adpilot"
    api_prefix: str = "/api"

    # --- PostgreSQL：客户、广告账户、归一化日指标、结算 ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "adpilot"
    postgres_user: str = "adpilot"
    postgres_password: SecretStr = Field(default=SecretStr(""))

    # --- MongoDB：平台原始报表快照，append-only ---
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_db: str = "adpilot_raw"
    mongo_user: str = "adpilot"
    mongo_password: SecretStr = Field(default=SecretStr(""))
    # 认证库。用 root 账号时是 admin（compose 里那份就是），连一个已经存在的
    # Mongo、账号建在业务库下时，要改成那个库名 —— 填错的症状是认证失败，
    # 而报错并不会告诉你它去哪个库找的账号。
    mongo_auth_source: str = "admin"

    # --- Redis：平台 API 限流令牌桶、热点指标缓存 ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    # Celery 的任务结果存在**另一个** db 号里，与缓存分开。理由是清缓存这件事迟早
    # 会发生（`FLUSHDB`），而它不该顺手把「那个任务到底成没成」一起清掉 —— 任务
    # 结果是给人查故障用的，缓存丢了自己会长回来。
    redis_celery_db: int = 1
    # Redis 这一项**空着是正常状态**，不是「凭据缺了个默认值」：compose 里的
    # Redis 只在内网监听、没开 requirepass。真给它配了密码就填在这里。
    redis_password: SecretStr = Field(default=SecretStr(""))

    # --- RabbitMQ：Celery 的消息 broker ---
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "adpilot"
    rabbitmq_password: SecretStr = Field(default=SecretStr(""))
    # vhost 是 RabbitMQ 侧的命名空间。自己起的那台用默认的 `/` 就行；连一个别人
    # 已经在用的 RabbitMQ 时必须换一个，否则两个系统的队列名会在同一个空间里撞。
    rabbitmq_vhost: str = "/"

    @property
    def postgres_dsn(self) -> str:
        """异步 SQLAlchemy DSN。"""
        return (
            f"postgresql+asyncpg://{_credentials(self.postgres_user, self.postgres_password)}"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def mongo_uri(self) -> str:
        """Mongo 连接串。

        末尾那个 `/` 不能省：没有它，`?authSource=` 会被当成路径的一部分。

        没配密码就**整段认证都不拼**（和 `redis_url` 同一个道理，但 Mongo 这边
        更凶）：`mongodb://adpilot:@host` 会让 pymongo 在 `AsyncIOMotorClient(...)`
        **构造时**就抛 `ConfigurationError: A password is required` —— 不是连接
        时，是构造时。于是整个进程起不来，存活探针永远等不到人应答。

        而按 [architecture.md](../../docs/code-rules/architecture.md) 的约定，
        构造客户端不等于连上去：某个依赖没配好或短暂挂掉时，进程仍然要能起来，
        「连不上」该由就绪探针报，不该由启动过程报。
        """
        credentials = (
            _credentials(self.mongo_user, self.mongo_password)
            if self.mongo_password.get_secret_value()
            else ""
        )
        return (
            f"mongodb://{credentials}"
            f"{self.mongo_host}:{self.mongo_port}/?authSource={self.mongo_auth_source}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接串（缓存与限流用的那个 db）。

        没配密码就不带认证段 —— 拼一个空的 `:@` 上去，客户端会真的发一次
        AUTH，然后被一台没开认证的 Redis 拒掉。
        """
        return self._redis_url(self.redis_db)

    @property
    def celery_result_backend(self) -> str:
        """Celery 存任务结果的地方，与缓存**不同一个 db**（见 `redis_celery_db`）。"""
        return self._redis_url(self.redis_celery_db)

    def _redis_url(self, db: int) -> str:
        password = self.redis_password.get_secret_value()
        credentials = f":{quote(password, safe='')}@" if password else ""
        return f"redis://{credentials}{self.redis_host}:{self.redis_port}/{db}"

    @property
    def celery_broker_url(self) -> str:
        """RabbitMQ 连接串。

        🔴 **vhost 必须 percent-encode。** 默认 vhost 就叫 `/`，直接拼进路径的话
        `amqp://u:p@host:5672//` 还能歪打正着，但换成任何带斜杠的 vhost 就会被
        urlparse 从那里切开，连到一个不存在的 vhost 上 —— 症状是 kombu 报
        `ACCESS_REFUSED`，而报错里不会提斜杠半个字。编码成 `%2F` 之后 kombu 会
        原样解回来（`kombu.utils.url.parse_url` 做 unquote）。
        """
        return (
            f"amqp://{_credentials(self.rabbitmq_user, self.rabbitmq_password)}"
            f"{self.rabbitmq_host}:{self.rabbitmq_port}/{quote(self.rabbitmq_vhost, safe='')}"
        )

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PROD


@lru_cache
def get_settings() -> Settings:
    """返回进程级配置单例。

    加缓存是为了只读一次环境变量；测试通过 FastAPI 的依赖覆盖来替换它，
    而不是去改模块级状态。
    """
    return Settings()
