"""Privacy startup tests use temporary profiles, fake ASR and no external network."""
import asyncio
import secrets
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.agent import ElderAgent
from src.core.memory import MemoryManager
from src.core.privacy import PrivacyGuard
from test_asr_safety import microphone  # noqa: F401 - shared fake hardware fixture


class FakeASR:
    """Assert that initialization and sensing see the shared authorization."""

    def __init__(self, order):
        self.order = order
        self.privacy = PrivacyGuard()
        self.init = AsyncMock(side_effect=self.initialize)
        self.listen = AsyncMock(return_value=None)
        self.close = AsyncMock()

    async def initialize(self):
        """No real hardware or model is loaded."""
        assert self.privacy.is_enabled()
        self.order.append("init")
        return True


def make_agent(tmp_path, response=True):
    """Each instance uses only this test's new profile directory."""
    order = []
    memory = MemoryManager(str(tmp_path))
    asr = FakeASR(order)

    async def fetch(elder_id):
        assert not asr.privacy.is_enabled()
        assert asr.listen.await_count == 0
        order.append("privacy")
        return response

    client = Mock(fetch_privacy=AsyncMock(side_effect=fetch), aclose=AsyncMock())
    agent = ElderAgent(memory=memory, asr=asr, edge_client=client, elder_id=1)
    return agent, asr, client, order


def test_default_closed_and_shared_guard(tmp_path):
    assert not PrivacyGuard().is_enabled()
    agent = ElderAgent(memory=MemoryManager(str(tmp_path)))
    assert agent.privacy is agent.asr.privacy
    assert not agent.privacy.is_enabled()


async def test_authorization_precedes_initialization_and_capture(tmp_path):
    agent, asr, client, order = make_agent(tmp_path)
    try:
        await agent._sense()
        asr.listen.assert_not_awaited()
        await agent.start()
        assert order[:2] == ["privacy", "init"]
        assert agent.capture_status == "ready"
        assert agent.privacy is asr.privacy
        await agent._sense()
        assert asr.listen.await_count >= 1
    finally:
        await agent.stop()
    asr.close.assert_awaited_once()
    client.aclose.assert_not_awaited()  # injected client belongs to the caller


@pytest.mark.parametrize("response", [False, None, "false", "true", 1, {}, [], {"enabled": True}])
async def test_unknown_or_denied_privacy_never_initializes_or_captures(tmp_path, response):
    agent, asr, _, _ = make_agent(tmp_path, response)
    try:
        await agent.start()
        await agent._sense()
        assert not agent.privacy.is_enabled()
        asr.init.assert_not_awaited()
        asr.listen.assert_not_awaited()
    finally:
        await agent.stop()


async def test_query_exception_fails_closed(tmp_path):
    agent, asr, client, _ = make_agent(tmp_path)
    client.fetch_privacy.side_effect = OSError("controlled network failure")
    try:
        await agent.start()
        assert agent.capture_status == "privacy_unavailable"
        assert not agent.privacy.is_enabled()
        asr.init.assert_not_awaited()
        asr.listen.assert_not_awaited()
    finally:
        await agent.stop()


@pytest.mark.parametrize("failure", [False, RuntimeError("controlled model failure")])
async def test_asr_initialization_failure_prevents_sensing(tmp_path, failure):
    agent, asr, _, _ = make_agent(tmp_path)
    if failure is False:
        asr.init.side_effect = None
        asr.init.return_value = False
    else:
        asr.init.side_effect = failure
    try:
        await agent.start()
        await agent._sense()
        assert agent.capture_status == "asr_unavailable"
        assert not agent.privacy.is_enabled()
        asr.listen.assert_not_awaited()
    finally:
        await agent.stop()


def test_local_revocation_survives_restart_and_requires_explicit_restore(tmp_path):
    first = PrivacyGuard(memory=MemoryManager(str(tmp_path)))
    first.apply_cloud(True)
    assert first.is_enabled()
    first.set_enabled(False)
    restored = PrivacyGuard(memory=MemoryManager(str(tmp_path)))
    restored.apply_cloud(True)
    assert not restored.is_enabled()
    restored.set_enabled(True)  # explicit local re-consent, with a current cloud grant
    assert restored.is_enabled()
    again = PrivacyGuard(memory=MemoryManager(str(tmp_path)))
    assert not again.is_enabled()  # saved local permission never auto-enables sensing
    again.apply_cloud(True)
    assert again.is_enabled()


async def test_local_revocation_wins_during_startup(tmp_path):
    memory = MemoryManager(str(tmp_path))
    PrivacyGuard(memory=memory).set_enabled(False)
    agent, asr, _, _ = make_agent(tmp_path)
    try:
        await agent.start()
        assert agent.capture_status == "local_revoked"
        asr.init.assert_not_awaited()
        asr.listen.assert_not_awaited()
    finally:
        await agent.stop()


def test_failed_query_cannot_clear_saved_revocation(tmp_path):
    guard = PrivacyGuard(memory=MemoryManager(str(tmp_path)))
    guard.set_enabled(False)
    guard.apply_cloud(None)
    guard.apply_cloud(True)
    assert not guard.is_enabled()
    restored = PrivacyGuard(memory=MemoryManager(str(tmp_path)))
    restored.apply_cloud(True)
    assert not restored.is_enabled()


def test_corrupt_local_marker_and_save_failure_are_closed(tmp_path, monkeypatch):
    memory = MemoryManager(str(tmp_path))
    memory.long_term["preferences"]["privacy_local_revoked"] = "false"
    guard = PrivacyGuard(memory=memory)
    guard.apply_cloud(True)
    assert not guard.is_enabled()
    monkeypatch.setattr(memory, "_save_long_term", Mock(side_effect=OSError("controlled storage failure")))
    with pytest.raises(ValueError):
        guard.set_enabled(True)
    assert not guard.is_enabled()


def test_expired_cloud_grant_cannot_be_restored_locally(monkeypatch):
    from src.core import privacy
    now = [100.0]
    monkeypatch.setattr(privacy.time, "monotonic", lambda: now[0])
    guard = PrivacyGuard()
    guard.apply_cloud(True, valid_for=5)
    assert guard.is_enabled()
    now[0] += 6
    assert not guard.is_enabled()
    guard.set_enabled(True)
    assert not guard.is_enabled()


async def test_refresh_revocation_and_failure_stop_capture(tmp_path):
    agent, asr, client, _ = make_agent(tmp_path)
    try:
        await agent.start()
        client.fetch_privacy.side_effect = None
        client.fetch_privacy.return_value = False
        await agent.sync_privacy()
        asr.listen.reset_mock()
        await agent._sense()
        asr.listen.assert_not_awaited()
        client.fetch_privacy.return_value = None
        await agent.sync_privacy()
        assert not agent.privacy.is_enabled()
    finally:
        await agent.stop()


def test_main_constructs_client_from_file_without_hardware(tmp_path, monkeypatch):
    from src import main
    token_file = tmp_path / "device.credential"
    token_file.write_text(".".join(secrets.token_urlsafe(24) for _ in range(3)), encoding="utf-8")
    monkeypatch.setenv("ELDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGE_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("EDGE_ELDER_ID", "1")
    monkeypatch.setenv("EDGE_DEVICE_TOKEN_FILE", str(token_file))
    agent = main.create_agent()
    assert agent.edge_client is not None
    assert agent.elder_id == 1
    assert agent.privacy is agent.asr.privacy
    assert not agent.privacy.is_enabled()


@pytest.mark.parametrize("case", ["missing", "unreadable"])
async def test_missing_credentials_degrade_without_asr(tmp_path, monkeypatch, case):
    from src import main
    from src.integration import edge
    monkeypatch.setenv("ELDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGE_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("EDGE_ELDER_ID", "1")
    monkeypatch.delenv("EDGE_DEVICE_TOKEN_FILE", raising=False)
    if case == "unreadable":
        monkeypatch.setenv("EDGE_DEVICE_TOKEN_FILE", str(tmp_path / "new.credential"))
        monkeypatch.setattr(edge, "_read_token_file", Mock(side_effect=edge.EdgeCredentialError("unavailable")))
    agent = main.create_agent()
    agent.asr.init = AsyncMock()
    agent.asr.listen = AsyncMock()
    try:
        await agent.start()
        assert agent.capture_status == "privacy_unavailable"
        agent.asr.init.assert_not_awaited()
        agent.asr.listen.assert_not_awaited()
    finally:
        await agent.stop()


async def test_main_cancellation_always_stops_agent(monkeypatch):
    from src import main
    started = asyncio.Event()

    async def start():
        started.set()

    agent = Mock(start=AsyncMock(side_effect=start), stop=AsyncMock())
    monkeypatch.setattr(main, "load_dotenv", Mock())
    monkeypatch.setattr(main, "create_agent", lambda: agent)
    task = asyncio.create_task(main.main())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    agent.stop.assert_awaited_once()


@pytest.mark.parametrize("case", ["allowed", "denied", "string", "missing", "wrong_elder", "bad_structure", "401", "403", "network"])
async def test_real_edge_client_drives_safe_asr_startup(tmp_path, microphone, case):
    """Exercise actual response validation, shared guard and fake microphone together."""
    from types import SimpleNamespace
    from src.ai.asr import ASRService
    from src.integration.edge import EdgeClient

    body = {"code": 200, "message": "success", "data": {"elder_id": 1, "enabled": True}}
    status = 200
    if case == "denied":
        body["data"]["enabled"] = False
    elif case == "string":
        body["data"]["enabled"] = "false"
    elif case == "missing":
        del body["data"]["enabled"]
    elif case == "wrong_elder":
        body["data"]["elder_id"] = 2
    elif case == "bad_structure":
        body = []
    elif case in ("401", "403"):
        status = int(case)
    response = SimpleNamespace(status_code=status, json=lambda: body)
    http = SimpleNamespace(get=AsyncMock(return_value=response))
    if case == "network":
        http.get.side_effect = OSError("controlled network failure")
    token = ".".join(secrets.token_urlsafe(24) for _ in range(3))
    client = EdgeClient(client=http, device_token=token)
    memory = MemoryManager(str(tmp_path))
    asr = ASRService()
    agent = ElderAgent(memory=memory, asr=asr, edge_client=client, elder_id=1)
    # Keep the actual start() ordering but avoid a competing background listen.
    agent._main_loop = AsyncMock()
    try:
        await agent.start()
        await agent._sense()
        module, audio, stream = microphone
        if case == "allowed":
            assert agent.capture_status == "ready"
            audio.open.assert_called_once()
            stream.read.assert_called_once()
        else:
            assert not agent.privacy.is_enabled()
            module.PyAudio.assert_not_called()
            audio.open.assert_not_called()
            stream.read.assert_not_called()
        headers = http.get.call_args.kwargs["headers"]
        valid = headers == {"Authorization": f"Bearer {token}"}
        assert valid
    finally:
        await agent.stop()


async def test_start_failure_closes_owned_resources(tmp_path):
    agent, asr, client, _ = make_agent(tmp_path)
    agent._owns_edge_client = True
    plugin = Mock(init=AsyncMock(side_effect=RuntimeError("controlled startup failure")),
                  start=AsyncMock(), stop=AsyncMock())
    agent.plugins = [plugin]
    with pytest.raises(RuntimeError, match="controlled startup failure"):
        await agent.start()
    assert not agent.privacy.is_enabled()
    assert not agent._running
    plugin.stop.assert_awaited_once()
    asr.close.assert_awaited_once()
    client.aclose.assert_awaited_once()
    await agent.stop()
    asr.close.assert_awaited_once()
    client.aclose.assert_awaited_once()


def test_atomic_revocation_failure_retains_existing_profile(tmp_path, monkeypatch):
    from src.core import memory as memory_module
    memory = MemoryManager(str(tmp_path))
    guard = PrivacyGuard(memory=memory)
    guard.set_enabled(False)
    before = memory.long_term_file.read_bytes()
    monkeypatch.setattr(memory_module.os, "replace", Mock(side_effect=OSError("controlled replace failure")))
    with pytest.raises(ValueError):
        guard.set_enabled(True)
    assert memory.long_term_file.read_bytes() == before
    assert list(tmp_path.glob(".profile-*")) == []
    restored = PrivacyGuard(memory=MemoryManager(str(tmp_path)))
    restored.apply_cloud(True)
    assert not restored.is_enabled()
