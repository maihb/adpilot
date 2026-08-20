"""连接串拼装的测试。

这里最容易出的错不是格式拼错 —— 那种一跑就发现。**是拼对了但没做 percent-encode**：
README 让人用 `openssl rand -base64 24` 生成密码，base64 的字符集里有 `/` 和 `+`，
一个 `/` 就能把 DSN 从那里截断，于是驱动去连一个不存在的主机，报错和「密码里有个
斜杠」看不出任何关系。

所以断言不写成「字符串等于某个字面量」，而是**拼进去再解析回来**：主机、端口、
密码三样都还原得出，才算这条串是对的。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from kombu.utils.url import parse_url as kombu_parse_url
from pydantic import SecretStr

from adpilot.config import Environment, Settings
from adpilot.db.broker import create_celery_app
from adpilot.db.mongo import create_client as create_mongo_client
from adpilot.db.postgres import create_engine
from adpilot.db.redis import create_client as create_redis_client

# 同时覆盖 base64 会产出的 `/` `+` `=`，以及 URI 里的两个分隔符 `:` `@`。
# 是一串刻意难看的假值，不是任何地方的真实凭据。
TRICKY = "a/b+c=d:e@f"


def _settings(
    *,
    mongo_auth_source: str = "admin",
    redis_password: str = "",
    rabbitmq_vhost: str = "/",
) -> Settings:
    # `_env_file=None` 关掉 .env 读取。不关的话，本机 .env 里的 REDIS_PORT 会
    # 补进没有显式传的字段，于是这些用例的结果取决于跑它的那台机器 —— 而单元
    # 测试必须在一台只装了 Python 的机器上也全绿。
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        postgres_host="pg.internal",
        postgres_password=SecretStr(TRICKY),
        mongo_host="mongo.internal",
        mongo_password=SecretStr(TRICKY),
        mongo_auth_source=mongo_auth_source,
        redis_host="redis.internal",
        redis_password=SecretStr(redis_password),
        rabbitmq_host="rabbit.internal",
        rabbitmq_password=SecretStr(TRICKY),
        rabbitmq_vhost=rabbitmq_vhost,
    )


def test_postgres_dsn_survives_a_password_full_of_separators() -> None:
    parsed = urlsplit(_settings().postgres_dsn)

    assert parsed.hostname == "pg.internal"  # 没编码的话会被 `/` 截断
    assert parsed.port == 5432
    assert unquote(parsed.password or "") == TRICKY
    assert parsed.path == "/adpilot"


def test_mongo_uri_survives_the_same_password() -> None:
    parsed = urlsplit(_settings().mongo_uri)

    assert parsed.hostname == "mongo.internal"
    assert parsed.port == 27017
    assert unquote(parsed.password or "") == TRICKY
    # authSource 必须真的落在 query 上；末尾少了那个 `/` 它会被当成路径
    assert parsed.query == "authSource=admin"


def test_mongo_auth_source_is_configurable() -> None:
    """账号建在业务库下时要改这个，填错的症状只是一句认证失败。"""
    assert urlsplit(_settings(mongo_auth_source="adpilot_raw").mongo_uri).query == (
        "authSource=adpilot_raw"
    )


def test_redis_url_omits_the_auth_section_when_there_is_no_password() -> None:
    """compose 里的 Redis 没开 requirepass —— 拼一个空的 `:@` 上去会让客户端
    真的发一次 AUTH，然后被拒。"""
    url = _settings().redis_url

    assert url == "redis://redis.internal:6379/0"
    assert "@" not in url


def test_redis_url_carries_an_encoded_password_when_one_is_set() -> None:
    parsed = urlsplit(_settings(redis_password=TRICKY).redis_url)

    assert parsed.hostname == "redis.internal"
    assert unquote(parsed.password or "") == TRICKY


def test_celery_broker_url_encodes_the_default_vhost() -> None:
    """默认 vhost 就叫 `/`，不编码的话它会被当成路径分隔符。

    断言解析回来的仍是 `/` —— 直接比字面量的话，`amqp://…:5672//` 这种「歪打
    正着」的写法也能过，而换成任何带斜杠的 vhost 它就错了。
    """
    parsed = kombu_parse_url(_settings().celery_broker_url)

    assert parsed["hostname"] == "rabbit.internal"
    assert parsed["port"] == 5672
    assert parsed["password"] == TRICKY
    assert parsed["virtual_host"] == "/"


def test_celery_broker_url_carries_a_custom_vhost() -> None:
    """连一台别人也在用的 RabbitMQ 时必须换 vhost，否则队列名会在同一个空间里撞。"""
    parsed = kombu_parse_url(_settings(rabbitmq_vhost="adpilot").celery_broker_url)

    assert parsed["virtual_host"] == "adpilot"


def test_celery_results_do_not_share_a_db_with_the_cache() -> None:
    """清缓存（FLUSHDB）不该顺手把「那个任务成没成」一起清掉。"""
    settings = _settings()

    assert urlsplit(settings.redis_url).path == "/0"
    assert urlsplit(settings.celery_result_backend).path == "/1"


def test_clients_construct_when_nothing_is_configured() -> None:
    """一个凭据都没配时，四个客户端仍然必须**构造得出来**。

    这条是 CI 上真红过一次才补的：`mongodb://adpilot:@host` 会让 pymongo 在
    构造时（不是连接时）抛 `ConfigurationError: A password is required`，于是
    进程起不来、存活探针永远等不到人应答 —— CI 的 docker job 正是这个场景，
    它只传 POSTGRES_PASSWORD 就把容器拉起来验存活。

    断言的是 architecture.md 那条约定：**构造客户端不等于连上去**。依赖没配好
    该由就绪探针报成 unhealthy，不该让启动过程崩掉。
    """
    bare = Settings(_env_file=None, environment=Environment.TEST)

    # 只构造，不发任何请求 —— 四个驱动都是懒连接
    create_engine(bare)
    create_mongo_client(bare)
    create_redis_client(bare)
    create_celery_app(bare).close()


def test_secrets_do_not_leak_through_repr() -> None:
    """一次日志、一次异常栈都可能把整个 Settings 打出来。"""
    dumped = repr(_settings())

    assert TRICKY not in dumped
    assert "pg.internal" in dumped  # 非凭据字段照常可见，否则这条断言就没意义
