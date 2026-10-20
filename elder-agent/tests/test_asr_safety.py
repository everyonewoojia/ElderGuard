"""Fake microphone/model tests; never import or open real audio hardware."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ai.asr import ASRService
from src.core.privacy import PrivacyGuard


@pytest.fixture()
def microphone(monkeypatch):
    """Replace both optional dependencies before initialization can import them."""
    stream = Mock()
    stream.read.return_value = b"audio"
    audio = Mock()
    audio.open.return_value = stream
    module = SimpleNamespace(PyAudio=Mock(return_value=audio), paInt16=8)
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    recognizer = Mock()
    recognizer.AcceptWaveform.return_value = False
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(
        Model=Mock(), KaldiRecognizer=Mock(return_value=recognizer)))
    from pathlib import Path
    original = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: True if str(path) == "models\\vosk-model-cn"
                        or path.as_posix() == "models/vosk-model-cn" else original(path))
    return module, audio, stream


async def test_unconfirmed_authorization_never_opens_or_reads(microphone):
    module, audio, stream = microphone
    service = ASRService()
    assert await service.init() is False
    assert await service.listen() is None
    module.PyAudio.assert_not_called()
    audio.open.assert_not_called()
    stream.read.assert_not_called()


async def test_authorized_but_uninitialized_never_opens(microphone):
    module, _, _ = microphone
    service = ASRService(PrivacyGuard(True))
    assert await service.listen() is None
    module.PyAudio.assert_not_called()


@pytest.mark.parametrize("failure", ["normal", "open", "read", "stop", "close"])
async def test_microphone_cleanup_on_every_path(microphone, failure):
    _, audio, stream = microphone
    service = ASRService(PrivacyGuard(True))
    assert await service.init() is True
    if failure == "open":
        audio.open.side_effect = OSError("controlled open failure")
    elif failure in ("read", "stop", "close"):
        target = stream.read if failure == "read" else stream.stop_stream if failure == "stop" else stream.close
        target.side_effect = OSError("controlled audio failure")
    data = await service.listen()
    assert data == (None if failure in ("open", "read") else b"audio")
    audio.terminate.assert_called_once()
    if failure != "open":
        stream.stop_stream.assert_called_once()
        stream.close.assert_called_once()


async def test_task_cancellation_releases_open_stream(microphone):
    _, audio, stream = microphone
    service = ASRService(PrivacyGuard(True))
    assert await service.init() is True
    opened = asyncio.Event()

    def open_stream(**kwargs):
        opened.set()
        return stream

    audio.open.side_effect = open_stream
    task = asyncio.create_task(service.listen())
    await opened.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stream.read.assert_not_called()
    stream.stop_stream.assert_called_once()
    stream.close.assert_called_once()
    audio.terminate.assert_called_once()


async def test_revoked_after_open_never_reads(microphone):
    _, audio, stream = microphone
    guard = PrivacyGuard(True)
    service = ASRService(guard)
    assert await service.init() is True

    def open_stream(**kwargs):
        guard.set_enabled(False)
        return stream

    audio.open.side_effect = open_stream
    assert await service.listen() is None
    stream.read.assert_not_called()
    stream.close.assert_called_once()
    audio.terminate.assert_called_once()


async def test_model_failure_does_not_enable_microphone(microphone, monkeypatch):
    module, _, _ = microphone
    sys.modules["vosk"].Model.side_effect = RuntimeError("controlled model failure")
    service = ASRService(PrivacyGuard(True))
    assert await service.init() is False
    assert await service.listen() is None
    assert service.status == "unavailable"
    module.PyAudio.assert_not_called()
