"""
cloud-server 测试基座
- 在导入 app 前设置测试环境变量（数据库、密钥），避免污染开发库
- 提供 TestClient 与建表/清表 fixture
"""
import os
import sys

# 必须在导入 app 之前设置，config.Settings 在模块导入时即读取这些值
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_elderguard.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-tests")

# 端云集成测试复用老人端 src 包（P6）：把 elder-agent 父目录加入 sys.path，
# 使 `import src`（src 是 elder-agent 内部的包）可解析。
_SRC_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "elder-agent"))
if _SRC_PARENT not in sys.path:
    sys.path.insert(0, _SRC_PARENT)

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import Base, SessionLocal, engine
from app.models.models import User, Elder, AlertLog, MonitorLog, ReminderTask


@pytest.fixture(scope="session")
def db_setup():
    """整个测试会话只建一次表，结束后清理（供 client/接口测试使用）"""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    """数据层测试：每个测试独立建表/清表，保证数据隔离"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_setup):
    """提供带生命周期管理的 TestClient，供接口测试使用"""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_clean():
    """接口测试：每个测试独立建表/清表 + TestClient，保证数据与状态隔离"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


PASSWORD = "pw123456"


@pytest.fixture()
def auth(client_clean):
    """P3 接口测试公共辅助：注册/登录/直插数据，基于每测试清库的 client_clean"""
    client = client_clean

    def register(username, phone):
        r = client.post("/api/v1/auth/register", json={
            "username": username, "password": PASSWORD, "phone": phone,
        })
        assert r.status_code == 200, r.text
        return r.json()

    def login(username):
        r = client.post("/api/v1/auth/login", data={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def create_elder(username, name, privacy_enabled=False):
        db = SessionLocal()
        owner = db.query(User).filter(User.username == username).first()
        elder = Elder(owner_id=owner.id, name=name, privacy_enabled=privacy_enabled)
        db.add(elder)
        db.commit()
        db.refresh(elder)
        db.close()
        return elder.id

    def create_task(elder_id, title="服药", task_type="用药提醒"):
        db = SessionLocal()
        task = ReminderTask(elder_id=elder_id, task_type=task_type, title=title, is_active=True)
        db.add(task)
        db.commit()
        db.refresh(task)
        db.close()
        return task.id

    def create_alert(elder_id, alert_type="fall", priority="P0", content="疑似跌倒"):
        db = SessionLocal()
        alert = AlertLog(elder_id=elder_id, alert_type=alert_type, priority=priority, content=content, status="pending")
        db.add(alert)
        db.commit()
        db.refresh(alert)
        db.close()
        return alert.id

    def create_log(elder_id, event_type="voice", content="对话"):
        db = SessionLocal()
        log = MonitorLog(elder_id=elder_id, event_type=event_type, content=content)
        db.add(log)
        db.commit()
        db.refresh(log)
        db.close()
        return log.id

    return {
        "client": client,
        "register": register,
        "login": login,
        "create_elder": create_elder,
        "create_task": create_task,
        "create_alert": create_alert,
        "create_log": create_log,
    }
