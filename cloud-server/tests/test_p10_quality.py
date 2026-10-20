"""P10 质量属性验收测试（云端）。

覆盖 §7.3/§7.4：XN02 ≥100 家庭并发接入与隔离、AQ01 越权回归、AQ04 数据完整性、
KY02 备份与恢复。P10-4 全量回归由三端全量 pytest 落地。
"""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.models import Elder
from app.services import storage


# ---------------------------------------------------------------------------
# P10-1 并发：XN02 模拟 ≥100 家庭并发接入 + 行级隔离（AQ01）
# ---------------------------------------------------------------------------
def test_xn02_100_families_concurrent_isolation(auth):
    """≥100 户并发接入：每户仅见自己绑定的老人，无串号/越权泄漏。"""
    client = auth["client"]
    n = 100
    tokens = {}
    for i in range(n):
        u = f"family{i}"
        phone = f"13{800000000 + i:08d}"
        auth["register"](u, phone)
        tokens[i] = auth["login"](u)
        auth["create_elder"](u, f"老人{i}")

    def fetch(i):
        r = client.get(
            "/api/v1/elder", headers={"Authorization": f"Bearer {tokens[i]}"}
        )
        return i, r

    results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, r in ex.map(fetch, range(n)):
            assert r.status_code == 200, r.text
            # GET /elder 直接返回 JSON 数组（response_model=List[ElderResponse]）
            elders = r.json()
            assert isinstance(elders, list)
            results[i] = [e["name"] for e in elders]

    for i in range(n):
        assert results[i] == [f"老人{i}"], f"家庭 {i} 看到异常数据（越权泄漏）"


# ---------------------------------------------------------------------------
# P10-3 安全合规：AQ01 越权访问 0 次（回归）
# ---------------------------------------------------------------------------
def test_aq01_cross_user_isolation(auth):
    """B 家属访问 A 的老人 → 403（越权 0 次）。"""
    auth["register"]("A", "13800001001")
    auth["register"]("B", "13800001002")
    elder_id = auth["create_elder"]("A", "张奶奶")
    tb = auth["login"]("B")
    r = auth["client"].get(
        f"/api/v1/elder/{elder_id}", headers={"Authorization": f"Bearer {tb}"}
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# P10-3 并发数据完整性：AQ04 写入无覆盖/无丢失
# ---------------------------------------------------------------------------
def test_aq04_write_integrity_no_overwrite(db):
    """批量写入不同家庭数据：id 唯一、无覆盖、无丢失。"""
    ids = set()
    for i in range(50):
        owner = storage.create_user(db, f"u_integ_{i}", "hash", role="family")
        e = storage.create_elder(db, owner.id, f"老人{i}")
        ids.add(e.id)
    assert len(ids) == 50


# ---------------------------------------------------------------------------
# P10-2 可靠性：KY02 数据库备份与恢复
# ---------------------------------------------------------------------------
def test_ky02_backup_and_restore(db, test_database):
    """复制数据库文件作为备份；模拟故障后用备份恢复可读原数据。"""
    owner = storage.create_user(db, "u_backup", "hash", role="family")
    elder = storage.create_elder(db, owner.id, "张奶奶")
    db.commit()

    elder_id = elder.id  # db.close() 前先取 id（关闭后对象不可再访问）
    db.close()
    backup = test_database.backup()

    engine2 = create_engine(f"sqlite:///{backup}")
    test_database.validate_backup_engine(engine2, backup)
    S = sessionmaker(bind=engine2)
    s = S()
    try:
        restored = s.query(Elder).filter(Elder.id == elder_id).first()
        assert restored is not None and restored.name == "张奶奶"
    finally:
        s.close()
        engine2.dispose()
