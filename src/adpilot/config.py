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

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# `AUTH_SECRET` 的最小长度。token 的 payload 是公开可读的，攻击者手里天然有一对
# （明文, 签名），所以密钥短了就是离线爆破的活靶子。`openssl rand -base64 32`
# 出来的串正好在这个量级之上。
AUTH_SECRET_MIN_LENGTH = 32


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

    # --- 告警通知：配了就推，没配就只记日志 ---
    #
    # 🔴 **这是凭据，不是普通 URL。** 企业微信 / 钉钉 / 飞书的 webhook 地址里直接
    # 带着 key，泄一次等于把发消息的权限交出去 —— 所以是 SecretStr，且任何日志、
    # 任何异常消息里都不许出现它。
    #
    # 空着是**正常状态**（和 `redis_password` 同理，不是「凭据缺了个默认值」）：
    # 开源使用者未必有 webhook，而一个「没配通知就起不来」的系统不符合「陌生人
    # clone 下来最容易跑起来」这条判断标准。
    alert_webhook_url: SecretStr = Field(default=SecretStr(""))

    # --- 认证：token 签发密钥 + 运营账号（D9 起）---
    #
    # 运营账号从环境变量来，**不建 `users` 表**：为一两个人做一套用户管理（增删改、
    # 改密码、找回密码、权限分级）是典型的范围蔓延，而它们每一个都要接口、要页面、
    # 要测试。多人共用一个运营账号在自托管小团队里是可接受的取舍，不是遗漏 ——
    # 见[设计文档第五节](../../docs/design/2026-08-21-client-auth.md)。
    auth_secret: SecretStr = Field(default=SecretStr(""))
    operator_username: str = "admin"
    # 🔴 存**哈希**不存明文。`.env` 一旦被谁贴进聊天窗口，明文就是直接可用的凭据。
    # 生成方式：`uv run python -m adpilot.auth.password`。
    operator_password_hash: SecretStr = Field(default=SecretStr(""))

    # --- LLM：任何讲 OpenAI 协议的服务（D13 起）---------------------------
    #
    # 不绑定任何一家：DeepSeek、Kimi、通义、Claude 与 Gemini 的兼容端点、本地
    # Ollama / vLLM 全都接得上，换供应商改这三个值就行（设计文档第十节第 2 条）。
    #
    # 三项都空着是**正常状态**：没配 LLM，日报照样出得来，只是那一行人话空着并
    # 标注「未生成」——数字部分是确定性的，不该被模型的可用性绑架。
    llm_base_url: str = ""
    llm_model: str = ""
    # 🔴 是凭据，所以是 SecretStr。**但空着不算「凭据留了默认值」**：本地跑的
    # Ollama / vLLM 根本不校验它，强制必填会把最容易上手的那条路堵死。判断「配没
    # 配 LLM」看的是 base_url 和 model（见 `llm_is_configured`），不看这一项。
    llm_api_key: SecretStr = Field(default=SecretStr(""))

    # 每天最多调几次。**不是省钱，是防失控**：一个写错的循环能在夜里把额度跑光，
    # 而自托管意味着花的是使用者自己的钱、没有人替他兜底。默认给得宽松 —— 日报
    # 一天一个账户一次，诊断是人点一下才有。
    llm_daily_call_limit: int = 200

    # 每百万 token 的价格，用来估成本。**默认 0 表示「没配单价」**，此时
    # `llm_calls.estimated_cost` 记 NULL 而不是 0 —— 0 的意思是「免费」，而实情
    # 是「不知道多少钱」，混起来会让月度成本统计凭空少一截。
    #
    # 币种由使用者自己心里有数：这里不记币种，因为它只跟一家供应商的账单对得上，
    # 而对账时人本来就知道自己用的是谁。金额一律 Decimal（conventions.md）。
    llm_input_cost_per_mtok: Decimal = Decimal("0")
    llm_output_cost_per_mtok: Decimal = Decimal("0")

    # --- 自动拉取平台数据（D19 起）-----------------------------------------
    #
    # 设计见 docs/design/2026-08-25-ads-api-fetch.md。整段空着是**正常状态**：
    # 没接 API 的实例照样跑，数据走 CSV 导入进来 —— 那是 MVP 一直支持的形态。

    # 🔴 平台 token 落库时的加密密钥。**丢了 = 所有已授权的凭据全部解不开**，
    # 只能把每个平台的授权流程重走一遍 —— 这一点和 AUTH_SECRET 有本质区别
    # （那个丢了只是所有人重新登录一次）。所以它必须进凭据存档，不能只活在
    # 部署机的 .env 里。
    #
    # 空着不算「凭据留了默认值」：`auth/crypto.py` 在密钥缺失或过短时**拒绝
    # 工作**（抛 CryptoNotConfiguredError），不会退化成「用空串当密钥」那种
    # 看起来一切正常、实际上等于没加密的状态。生成：openssl rand -base64 32
    credentials_secret: SecretStr = Field(default=SecretStr(""))

    # TikTok 开发者应用。App ID 不是秘密（它出现在授权 URL 里，用户浏览器看得
    # 到），secret 是。
    tiktok_app_id: str = ""
    tiktok_app_secret: SecretStr = Field(default=SecretStr(""))

    # 空着用 provider 内置的生产地址。**填了就是切沙盒** —— 审核通过之前只有
    # 沙盒能用，而两者的差别应该只有这一个值。
    tiktok_api_base_url: str = ""

    # 除核心指标外还要请求哪些，逗号分隔。
    #
    # 🔴 收入类指标（GMV）最容易随平台功能改名，而**请求一个不存在的 metric 是
    # 整个请求 400** —— 一个字段名写错，当天所有账户一行数据都拉不到。所以它们
    # 走配置而不是写死：沙盒实测确认了名字，填进来就生效，不必改代码重新部署。
    # 理由详见 providers/tiktok.py 的模块 docstring。
    tiktok_extra_metrics: str = ""

    # OAuth 回调地址的前缀（`https://adpilot.example.com`，不带末尾斜杠）。
    #
    # ⚠️ **必须和开发者后台里填的那个完全一致**，差一个字符平台就拒绝跳转，
    # 而报错发生在平台那边、我们这边一行日志都不会有。它单独成项而不是从请求的
    # Host 头推断：Host 是客户端说了算的，用它拼回调地址等于让请求方决定 token
    # 往哪送。
    oauth_redirect_base_url: str = ""

    # 每次拉最近几天，**不是只拉昨天**。平台数据在若干天内还会变（归因回传、
    # 无效流量剔除、汇率重算），只拉昨天那些修正就永远进不来了。
    #
    # 这一条几乎不花额外成本：快照 append-only（多拉一天就是多一条快照），
    # 归一化按唯一键 upsert（同一天拉三次也不会变成三倍花费）—— 滚动窗口在这套
    # 结构上是免费的，那正是当初养第二个数据库换来的东西。
    fetch_window_days: int = 3

    # 超过这么多小时没成功拉到数就开告警。
    #
    # 定在 26 而不是 24：日切、平台自己的延迟、夏令时那天不是 24 小时，都会让
    # 「上次成功」的间隔天然地略超一天。卡在 24 会让告警每周误报几次，而一条
    # 每周误报的告警，三周之后就没人看了。
    fetch_stale_hours: int = 26

    @property
    def llm_is_configured(self) -> bool:
        """调得出模型没有。判断收口在这里，调用方不去比对空字符串。

        **不看 `llm_api_key`**：本地推理服务不需要它，而把它算进判据会让「用
        Ollama 跑通全链」变成一件要先编个假 key 的事。
        """
        return bool(self.llm_base_url and self.llm_model)

    @property
    def llm_prices_are_configured(self) -> bool:
        """填了单价没有。没填就不估成本，记 NULL（见上面那两项）。"""
        return bool(self.llm_input_cost_per_mtok or self.llm_output_cost_per_mtok)

    @property
    def tiktok_is_configured(self) -> bool:
        """换得出 token 没有。判断收口在这里，调用方不去比对空字符串。

        **不看 `credentials_secret`**：那一项缺失是另一种故障（存不下也读不出
        已有凭据），由 `auth/crypto.py` 当场拒绝工作并说清楚缺哪个环境变量。
        混进这个判据会让「没配 TikTok 应用」和「没配加密密钥」报出同一句话。
        """
        return bool(self.tiktok_app_id and self.tiktok_app_secret.get_secret_value())

    @property
    def tiktok_extra_metric_names(self) -> tuple[str, ...]:
        """把逗号分隔的配置摊成清单，顺手去掉空项和多余空格。

        容忍尾随逗号和空格是有意的：这个值是人从平台文档里一个个拷过来拼的，
        而一个多出来的空字符串会让整次请求 400 —— 报错还只会说「metrics 不合法」。
        """
        return tuple(name.strip() for name in self.tiktok_extra_metrics.split(",") if name.strip())

    @property
    def alerts_are_pushed(self) -> bool:
        """配了 webhook 没有。判断收口在这里，调用方不去比对空字符串。"""
        return bool(self.alert_webhook_url.get_secret_value())

    @property
    def auth_is_configured(self) -> bool:
        """签得出 token 没有。判断收口在这里，调用方不去比对空字符串。"""
        return bool(self.auth_secret.get_secret_value())

    @model_validator(mode="after")
    def _require_auth_in_production(self) -> Settings:
        """生产环境必须配齐认证，非生产放行。

        **为什么不是一律必填。** 这两项空着的失败方向不一样，而
        [CLAUDE.md](../../CLAUDE.md) 硬规矩 2 真正要挡的是「空值也能放行」：

        * `OPERATOR_PASSWORD_HASH` 空着 = **谁也登不进来**（`auth/password.py` 的
          `verify_password` 直接返回 False）。这是安全方向的失败。
        * `AUTH_SECRET` 空着 = 签发和校验**双双拒绝工作**
          （`auth/token.py` 的 `_key` 抛 `AuthNotConfiguredError`），不会退化成
          「用空串当密钥」那种「看起来一切正常、实际上等于没有认证」。

        既然两者都不会静默放行，就没必要让一台还没填 `.env` 的机器连进程都起不来
        —— 那会一起打掉「陌生人 clone 五分钟跑起来」和「只装了 Python 的机器上
        `uv run pytest` 全绿」这两条既有约定（`main.py` 在 import 时就会构造
        应用，配置校验失败等于整套单元测试崩在收集阶段）。

        到了 `prod` 判据翻转：那里没有「还没配」这种正当状态，缺了就大声失败。
        判据用 `ENVIRONMENT` 与 `seed.py` 拒绝在生产执行是同一个套路。
        """
        if not self.is_production:
            return self

        secret = self.auth_secret.get_secret_value()
        if len(secret) < AUTH_SECRET_MIN_LENGTH:
            raise ValueError(
                f"生产环境必须设置 AUTH_SECRET，且至少 {AUTH_SECRET_MIN_LENGTH} 个字符："
                "openssl rand -base64 32"
            )
        if not self.operator_password_hash.get_secret_value():
            raise ValueError(
                "生产环境必须设置 OPERATOR_PASSWORD_HASH：uv run python -m adpilot.auth.password"
            )
        return self

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
