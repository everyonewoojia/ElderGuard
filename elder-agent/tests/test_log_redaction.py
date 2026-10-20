"""Real application methods with synthetic inputs; no network or audio devices."""
import asyncio
from datetime import datetime, timedelta
import importlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest

from src.ai.asr import ASRService
from src.ai.call import CallService
from src.ai.intent import IntentService
from src.ai.tts import TTSService
from src.core.event_bus import EventBus
from src.core.privacy import PrivacyGuard
from src.integration.edge import EdgeClient
from src.plugins.command import CommandPlugin
from src.plugins.reminder import ReminderPlugin


ASR = "SYNTHETIC_ASR_CANARY_7F21"
TTS = "SYNTHETIC_TTS_CANARY_8A32"
INTENT = "SYNTHETIC_INTENT_CANARY_9B43"
TARGET = "SYNTHETIC_TARGET_CANARY_1C54"
URL = "SYNTHETIC_URL_CANARY_2D65"
COMMAND = "SYNTHETIC_COMMAND_CANARY_3E76"
TITLE = "SYNTHETIC_TITLE_CANARY_4F87"
TOKEN = "SYNTHETIC_TOKEN_HEADER.SYNTHETIC_TOKEN_PAYLOAD.SYNTHETIC_TOKEN_SIGNATURE"
MARKERS = (ASR, TTS, INTENT, TARGET, URL, COMMAND, TITLE, TOKEN)


def safe_records(caplog, event=None, *, logger=None, arguments=()):
    """Inspect raw fields, extras and rendered logs, keeping useful diagnostics."""
    for record in caplog.records:
        assert record.exc_info is None
        assert record.stack_info is None
        views = (str(record.msg), repr(record.args), record.getMessage(), repr(record.__dict__))
        assert all(marker not in view for marker in MARKERS for view in views)
    assert all(marker not in caplog.text for marker in MARKERS)
    if event is not None:
        matches = [r for r in caplog.records if r.name == logger and r.msg == event]
        assert len(matches) == 1, "the controlled diagnostic must remain present"
        assert matches[0].args == arguments


@pytest.mark.parametrize("outcome", ["recognized", "empty", "partial", "failure", "unauthorized"])
async def test_asr_real_recognize_preserves_results_without_logging_text(outcome, caplog):
    """Exercise the accepted-result branch rather than replacing recognize()."""
    caplog.set_level(logging.INFO)
    service = ASRService(PrivacyGuard(outcome != "unauthorized"))
    recognizer = Mock()
    recognizer.AcceptWaveform.return_value = outcome != "partial"
    recognizer.FinalResult.return_value = json.dumps({"text": "" if outcome == "empty" else ASR})
    service._recognizer = recognizer
    error = RuntimeError(ASR)
    if outcome == "failure":
        recognizer.AcceptWaveform.side_effect = error
        with pytest.raises(RuntimeError) as captured:
            await service.recognize(b"synthetic audio")
        assert captured.value is error
        assert caplog.records == []
    else:
        result = await service.recognize(b"synthetic audio")
        assert result == (ASR if outcome == "recognized" else None)
        if outcome == "recognized":
            safe_records(caplog, "ASR recognition completed", logger="src.ai.asr")
        if outcome == "unauthorized":
            recognizer.AcceptWaveform.assert_not_called()
            assert service.privacy.capture_count == 0
        elif outcome == "partial":
            recognizer.FinalResult.assert_not_called()
    safe_records(caplog)
    await service.close()
    assert service._recognizer is None and service.status == "closed"


@pytest.mark.parametrize("outcome", ["audio", "none", "empty", "synthesis-error", "playback-error",
                                    "synthesis-cancel", "playback-cancel"])
async def test_tts_real_speak_preserves_arguments_order_and_errors(outcome, caplog):
    """Keep synthesis, playback and cancellation semantics independently of logs."""
    caplog.set_level(logging.INFO)
    service = TTSService()
    audio = b"synthetic audio"
    service._synthesize = AsyncMock(return_value=None if outcome == "none" else b"" if outcome == "empty" else audio)
    service._play = AsyncMock()
    ordered = Mock()
    ordered.attach_mock(service._synthesize, "synthesize")
    ordered.attach_mock(service._play, "play")
    error = None
    if "error" in outcome or "cancel" in outcome:
        error = asyncio.CancelledError(TTS) if "cancel" in outcome else RuntimeError(TTS)
        (service._synthesize if outcome.startswith("synthesis") else service._play).side_effect = error
        with pytest.raises(type(error)) as captured:
            await service.speak(TTS)
        assert captured.value is error
    else:
        assert await service.speak(TTS) is (outcome == "audio")
    service._synthesize.assert_awaited_once_with(TTS)
    played = outcome == "audio" or outcome.startswith("playback")
    if played:
        service._play.assert_awaited_once_with(audio)
    else:
        service._play.assert_not_awaited()
    assert ordered.mock_calls == [call.synthesize(TTS)] + ([call.play(audio)] if played else [])
    safe_records(caplog, "Speech synthesis requested", logger="src.ai.tts")


class UnprintableError(Exception):
    """Neither error formatting method may be touched by application diagnostics."""

    def __str__(self):
        raise RuntimeError(INTENT)

    def __repr__(self):
        raise RuntimeError(INTENT)


@pytest.mark.parametrize("kind", ["timeout", "connection", "runtime", "empty", "unprintable", "chain"])
async def test_intent_failure_keeps_real_rule_fallback_and_safe_log(kind, caplog):
    """Use the actual parse/rule implementations, including hostile exceptions."""
    caplog.set_level(logging.INFO)
    error = {"timeout": TimeoutError, "connection": ConnectionError,
             "unprintable": UnprintableError}.get(kind, RuntimeError)("" if kind == "empty" else INTENT)
    if kind == "chain":
        error.__cause__ = ValueError(TOKEN)
        error.__context__ = ConnectionError(URL)
    service = IntentService()
    service._cloud_parse = AsyncMock(side_effect=error)
    text = "救命 " + INTENT
    result = await service.parse(text)
    assert result == {"type": "help", "priority": "P0", "content": text}
    service._cloud_parse.assert_awaited_once_with(text)
    safe_records(caplog, "Intent cloud parsing unavailable; using local rules", logger="src.ai.intent")


@pytest.mark.parametrize("boundary", ["cancel", "rule-error"])
async def test_intent_does_not_swallow_cancellation_or_rule_errors(boundary, caplog):
    """Logging must not extend the original try/except business boundary."""
    caplog.set_level(logging.INFO)
    service = IntentService()
    error = asyncio.CancelledError(INTENT) if boundary == "cancel" else RuntimeError(INTENT)
    service._cloud_parse = AsyncMock(side_effect=error if boundary == "cancel" else ConnectionError(INTENT))
    service._rule_parse = Mock(side_effect=error)
    with pytest.raises(type(error)) as captured:
        await service.parse(INTENT)
    assert captured.value is error
    if boundary == "cancel":
        service._rule_parse.assert_not_called()
        assert caplog.records == []
    else:
        service._rule_parse.assert_called_once_with(INTENT)
    safe_records(caplog)


@pytest.mark.parametrize("command", ["unmatched", "help", "reminder", "time"])
async def test_command_real_dispatch_never_logs_input(command, caplog):
    """Compatibility entry tests do not claim default microphone reachability."""
    caplog.set_level(logging.INFO)
    plugin = CommandPlugin(EventBus())
    text = {"help": "帮忙", "reminder": "提醒", "time": "几点"}.get(command, "未知") + COMMAND
    assert await plugin.handle_event({"payload": {"text": text}}) is None
    assert plugin.executed == ([] if command == "unmatched" else [command])
    if command == "unmatched":
        safe_records(caplog, "Command unmatched", logger="src.plugins.command")
    else:
        safe_records(caplog, f"执行技能: {command}", logger="src.plugins.command")


@pytest.mark.parametrize("target", ["valid", "empty", "whitespace", "none"])
async def test_call_keeps_simulated_target_and_results_without_logging_it(target, caplog):
    """The service retains its target; no real telephony interface exists here."""
    caplog.set_level(logging.INFO)
    value = {"valid": TARGET, "empty": "", "whitespace": "   ", "none": None}[target]
    service = CallService()
    assert await service.connect(value) is (target == "valid")
    if target == "valid":
        assert service.target() is value and service.is_connected()
        safe_records(caplog, "Call connected", logger="src.ai.call")
        assert await service.disconnect() is True
        assert service.target() is None and not service.is_connected()
    else:
        assert not service.is_connected()
        assert await service.disconnect() is False
    safe_records(caplog)


@pytest.mark.parametrize("kind", ["scheduled", "sedentary"])
async def test_reminder_preserves_title_schedule_state_and_cancellation(kind, caplog):
    """Business storage keeps the title while application logs exclude it."""
    caplog.set_level(logging.INFO)
    now = [datetime(2026, 1, 1, 8)]
    memory = SimpleNamespace(save=AsyncMock())
    plugin = ReminderPlugin(EventBus(), memory=memory, now=lambda: now[0])
    await plugin.handle_event({"payload": {"task_type": "久坐提醒" if kind == "sedentary" else "用药提醒",
                                          "title": TITLE, "schedule_time": "08:30"}})
    if kind == "sedentary":
        assert await plugin.check_sedentary() is False
        now[0] += timedelta(hours=4)
        assert await plugin.check_sedentary() is True
        assert await plugin.check_sedentary() is False
        memory.save.assert_awaited_once()
        assert TITLE in memory.save.await_args.args[0]["payload"]["content"]
    tasks = plugin.get_task_status()
    assert list(tasks) == ["task_0"]
    assert tasks["task_0"] == {"title": TITLE, "status": "pending",
                                "schedule_time": "08:30" if kind == "scheduled" else None}
    await plugin.mark_completed("task_0")
    assert plugin.get_task_status("task_0")["status"] == "completed"
    await plugin.mark_pending("task_0")
    assert plugin.get_task_status("task_0")["status"] == "pending"
    timer = Mock()
    plugin._timers["synthetic"] = timer
    await plugin.stop()
    timer.cancel.assert_called_once_with()
    safe_records(caplog, "Reminder task received", logger="src.plugins.reminder")
    safe_records(caplog, "Reminder scheduled: %s", logger="src.plugins.reminder", arguments=("task_0",))


@pytest.mark.parametrize("repetitions", [1, 2])
async def test_real_httpx_url_logs_are_blocked_but_application_status_remains(repetitions, caplog):
    """Real AsyncClient/MockTransport emits its own INFO URL unless policy works."""
    from src import main

    caplog.set_level(logging.INFO)
    logger = logging.getLogger("httpx")
    child = logging.getLogger("httpx.synthetic_child")
    previous = (logger.level, logger.disabled, logger.propagate, child.level)
    handlers = list(logging.getLogger().handlers)
    try:
        logger.disabled, logger.propagate = False, True
        logger.setLevel(logging.INFO)
        child.setLevel(logging.NOTSET)
        for _ in range(repetitions):
            importlib.reload(main)  # Definitions only: no main(), dotenv or Agent construction.
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as http:
                client = EdgeClient(base_url="https://example.invalid/" + URL, device_token=TOKEN, client=http)
                assert await client.fetch_privacy(123456789) is None
            child.info(URL)
        assert logger.getEffectiveLevel() == logging.WARNING
        assert child.getEffectiveLevel() == logging.WARNING
        assert list(logging.getLogger().handlers) == handlers
        assert not [r for r in caplog.records if r.name == "httpx" or r.name.startswith("httpx.")]
        status = [r for r in caplog.records if r.name == "src.integration.edge"]
        assert len(status) == repetitions
        assert all(r.msg == "edge request rejected: status=%d" and r.args == (503,) for r in status)
        safe_records(caplog)
    finally:
        logger.setLevel(previous[0])
        logger.disabled, logger.propagate = previous[1:3]
        child.setLevel(previous[3])
