"""
smoke 测试（P0 基线）
- 仅验证应用可导入、可构造、健康检查可用
- 本阶段不承载任何业务测试，业务测试从 P1 起逐步加入
"""


def test_app_importable():
    """应用模块可正常导入且标题正确"""
    from app.main import app

    assert app.title == "银发守护 Agent 中枢服务"
    assert app.version == "0.1.0"


def test_health_endpoint(client):
    """健康检查接口返回 200 与固定状态"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_openapi_docs_generated(client):
    """OpenAPI 文档自动生成，包含 v1 前缀路由"""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    # auth 登录为确定路径；tasks 的 POST 路由可能带尾斜杠，按前缀匹配更稳健
    assert "/api/v1/auth/login" in paths
    assert any(p.rstrip("/") == "/api/v1/tasks" for p in paths)
