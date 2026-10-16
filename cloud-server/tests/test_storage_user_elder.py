"""P1-2 用户档案存储：User/Elder 的 CRUD 服务（M-DATA-01）"""
from app.services import storage


def test_create_and_get_user_by_username(db):
    user = storage.create_user(db, username="carol", password_hash="h", role="family", phone="139")
    assert user.id is not None
    assert storage.get_user_by_username(db, "carol").id == user.id
    assert storage.get_user_by_id(db, user.id).username == "carol"


def test_create_elder_and_get(db):
    owner = storage.create_user(db, username="dad", password_hash="h")
    elder = storage.create_elder(db, owner_id=owner.id, name="王爷爷", age=78)
    got = storage.get_elder(db, elder.id)
    assert got.name == "王爷爷" and got.age == 78


def test_list_elders_by_owner_isolation(db):
    """越权隔离前置：只返回自己绑定的老人"""
    o1 = storage.create_user(db, username="o1", password_hash="h")
    o2 = storage.create_user(db, username="o2", password_hash="h")
    storage.create_elder(db, owner_id=o1.id, name="甲")
    storage.create_elder(db, owner_id=o2.id, name="乙")
    names = [e.name for e in storage.list_elders_by_owner(db, o1.id)]
    assert names == ["甲"]


def test_update_elder_privacy(db):
    owner = storage.create_user(db, username="o3", password_hash="h")
    elder = storage.create_elder(db, owner_id=owner.id, name="x")
    assert elder.privacy_enabled is False
    updated = storage.update_elder_privacy(db, elder.id, True)
    assert updated.privacy_enabled is True


def test_update_elder_privacy_not_found(db):
    """更新不存在的老人返回 None"""
    assert storage.update_elder_privacy(db, 9999, True) is None
