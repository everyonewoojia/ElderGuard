"""Real OS file locks and child exits; every file is inside a fresh pytest sandbox."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_alert_outbox import event, queue


DRIVER = '''
import json, os, sys
from src.core.alert_outbox import AlertOutbox, OutboxError
try:
    q = AlertOutbox(sys.argv[1], device_id="device-a", elder_id=1,
                    cloud_url="https://example.invalid")
except OutboxError as exc:
    print(exc.code)
    raise SystemExit(0 if exc.code == "owner_busy" else 4)
if sys.argv[2] == "probe":
    q.close()
    print("acquired")
else:
    item = json.loads(sys.argv[3])
    q.enqueue(item)
    if sys.argv[2] == "sending":
        q.claim(item["eventId"])
    if sys.argv[2] == "terminate":
        print("READY", flush=True)
        sys.stdin.read()
    elif sys.argv[2] == "normal":
        q.close()
    else:
        os._exit(23)
'''


def environment(path):
    """Children do not read credential configuration or create flat temp resources."""
    result = os.environ.copy()
    result.pop("EDGE_DEVICE_TOKEN_FILE", None)
    result["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result["TEMP"] = result["TMP"] = str(path)
    return result


def test_second_process_is_blocked_then_can_acquire(tmp_path):
    command = [sys.executable, "-B", "-c", DRIVER, str(tmp_path), "probe"]
    with queue(tmp_path):
        blocked = subprocess.run(command, cwd=tmp_path, env=environment(tmp_path),
                                 capture_output=True, text=True, timeout=30)
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "owner_busy"
    released = subprocess.run(command, cwd=tmp_path, env=environment(tmp_path),
                              capture_output=True, text=True, timeout=30)
    assert released.returncode == 0, released.stderr
    assert released.stdout.strip() == "acquired"


@pytest.mark.parametrize("mode", ["normal", "saved", "sending", "terminate"])
def test_child_exit_releases_lock_and_restores_identity(tmp_path, mode):
    item = event()
    command = [sys.executable, "-B", "-c", DRIVER, str(tmp_path), mode, json.dumps(item)]
    if mode == "terminate":
        child = subprocess.Popen(command, cwd=tmp_path, env=environment(tmp_path),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "READY"
            child.terminate()  # Only this explicitly owned child process.
            child.communicate(timeout=30)
            assert child.returncode != 0
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=30)
    else:
        result = subprocess.run(command, cwd=tmp_path, env=environment(tmp_path),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == (0 if mode == "normal" else 23), result.stderr
    with queue(tmp_path) as recovered:
        record = recovered.records()[0]
        assert record["event_id"] == item["eventId"]
        assert record["device_id"] == "device-a"
        assert record["elder_id"] == 1
        assert record["content"] == item["payload"]["content"]
        assert record["state"] == "pending"
        assert record["attempts"] == (1 if mode == "sending" else 0)
