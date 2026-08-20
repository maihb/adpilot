"""存活/就绪探针的测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adpilot.config import Environment, Settings
from adpilot.main import create_app


def test_liveness_ignores_backing_services(offline_client: TestClient) -> None:
    """所有依赖都挂着时，存活探针仍必须是绿的。

    这正是两个探针拆开的意义：存活探针要是去查数据库，一次短暂抖动就会触发重启
    循环，把本来能自愈的小故障变成自己造出来的事故。
    """
    response = offline_client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_503_when_dependencies_are_down(
    offline_client: TestClient,
) -> None:
    """依赖不可用时，就绪探针要干净降级，而不是卡住或抛异常。"""
    response = offline_client.get("/api/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"

    reported = {dep["name"] for dep in body["dependencies"]}
    assert reported == {"postgres", "mongodb", "redis", "rabbitmq"}
    assert all(dep["healthy"] is False for dep in body["dependencies"])


def test_readiness_names_the_failing_dependency(offline_client: TestClient) -> None:
    """每个不健康的依赖都要带一条 detail 供运维定位。

    detail 只放异常类名，不放驱动原始报错 —— 后者可能把 DSN 带出来，而这个接口
    通常不需要认证就能访问。
    """
    body = offline_client.get("/api/health/ready").json()

    for dep in body["dependencies"]:
        assert dep["detail"], f"{dep['name']} 没有给出 detail"
        assert "://" not in dep["detail"]


def test_openapi_schema_is_generated(offline_client: TestClient) -> None:
    """生成的 schema 是对外产物，一旦坏掉必须让 CI 红灯。"""
    response = offline_client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "adpilot"
    assert "/api/health/ready" in schema["paths"]


def test_production_hides_the_schema_and_the_docs(offline_settings: Settings) -> None:
    """生产环境关掉 `/docs` 和 `/openapi.json`，**但探针照常开着**。

    关掉不是指望靠它藏住什么 —— 仓库是公开的，接口清单本来就在源码里。关掉的是
    一个免认证、能一次性枚举出全部路由与出入参形状的入口。前端的类型生成读的是
    本地或 CI 起的非生产实例，不受影响。
    """
    production = offline_settings.model_copy(update={"environment": Environment.PROD})

    with TestClient(create_app(production)) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/api/health/live").status_code == 200


@pytest.mark.integration
def test_readiness_is_green_against_real_services(live_client: TestClient) -> None:
    """PostgreSQL / MongoDB / Redis / RabbitMQ 真的起着时，就绪探针应报 ready。"""
    response = live_client.get("/api/health/ready")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert all(dep["healthy"] for dep in body["dependencies"])
