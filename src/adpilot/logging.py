"""结构化日志配置。

生产环境输出 JSON，方便日志系统按字段索引；开发环境输出带颜色的键值对，方便人
直接读。两种模式下调用点写法完全一致 —— `log.info("event_name", key=value)`，
事件名始终可 grep，而不会被拼进一句自然语言里埋掉。
"""

from __future__ import annotations

import logging
import sys

import structlog

from adpilot.config import Settings


def configure_logging(settings: Settings) -> None:
    """配置 structlog 以及与标准库 logging 的桥接。"""
    level = logging.DEBUG if settings.debug else logging.INFO

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
