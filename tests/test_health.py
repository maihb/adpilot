"""存活/就绪探针的测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
    assert reported == {"postgres", "mongodb", "redis"}
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


@pytest.mark.integration
def test_readiness_is_green_against_real_services(live_client: TestClient) -> None:
    """PostgreSQL / MongoDB / Redis 真的起着时，就绪探针应报 ready。"""
    response = live_client.get("/api/health/ready")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert all(dep["healthy"] for dep in body["dependencies"])
