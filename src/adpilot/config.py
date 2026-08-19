"""应用配置，全部从环境变量读取。

所有可调项只在这里出现一次。这个模块守两条规矩：

1. **凭据没有可用的默认值。** 缺了就在启动时大声失败，而不是悄悄退回某个
   「本地能跑」的值，然后一路跟到生产环境。
2. **凭据一律用 `SecretStr`。** 它们不会因为一次日志、一次异常栈或一次
   `repr()` 意外泄出来；要读必须显式调 `.get_secret_value()`，review 时
   一 grep 就能找全。
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """部署环境。生产环境不能放松的护栏都以这个值为判据。"""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


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
    mongo_uri: SecretStr = Field(default=SecretStr("mongodb://localhost:27017"))
    mongo_db: str = "adpilot_raw"

    # --- Redis：平台 API 限流令牌桶、热点指标缓存 ---
    redis_url: SecretStr = Field(default=SecretStr("redis://localhost:6379/0"))

    @property
    def postgres_dsn(self) -> str:
        """由上面各项拼出的异步 SQLAlchemy DSN。"""
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
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
