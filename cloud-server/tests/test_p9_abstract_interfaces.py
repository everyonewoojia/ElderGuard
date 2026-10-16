"""P9 P2 接口预留契约测试（云端）：社区后台对接 / 社区批量存储抽象接口。"""
import pytest

from app.services import batch_storage, community


class TestCommunityBackend:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            community.CommunityBackend()

    def test_concrete_backend_pushes(self):
        class C(community.CommunityBackend):
            def push_alert(self, alert): return True
            def sync_status(self): return {"ok": True}
        c = C()
        assert c.push_alert({"id": 1}) is True
        assert c.sync_status()["ok"] is True

    def test_contract_methods_present(self):
        for name in ("push_alert", "sync_status"):
            assert hasattr(community.CommunityBackend, name)


class TestBatchStorage:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            batch_storage.BatchStorage()

    def test_concrete_storage_batches(self):
        class B(batch_storage.BatchStorage):
            def batch_upsert(self, records): return len(records)
            def flush(self): return 0
        assert B().batch_upsert([{}, {}]) == 2

    def test_contract_methods_present(self):
        for name in ("batch_upsert", "flush"):
            assert hasattr(batch_storage.BatchStorage, name)
