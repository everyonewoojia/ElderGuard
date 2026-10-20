"""Real WS messages and matching REST JSON drive the actual miniapp consumer."""
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest


@pytest.mark.parametrize('delivery', ['legacy-replay', 'durable-worker'])
def test_route_message_reaches_miniapp(auth, device_credentials, delivery) -> None:
    """Use only registered fixtures and ASGI transport; Node fakes WeChat transport."""
    auth['register']('ws-contract-family', '13800007772')
    elder = auth['create_elder']('ws-contract-family', 'synthetic')
    client = auth['client']
    family_header = {'Authorization': f"Bearer {auth['login']('ws-contract-family')}"}
    if delivery == 'legacy-replay':
        alert = auth['create_alert'](elder)
    else:
        device = device_credentials(elder)
        admitted = client.post('/api/v1/edge/alerts', json={
            'event_id': str(uuid4()), 'elder_id': elder, 'alert_type': 'fall',
            'priority': 'P0', 'content': 'synthetic',
        }, headers={'Authorization': f'Bearer {device}'})
        assert admitted.status_code == 200
        alert = admitted.json()['data']['alert_id']
    with client.websocket_connect('/api/v1/ws/alerts', headers=family_header) as socket:
        future = socket.portal.start_task_soon(socket._send_rx.receive)
        try:
            frame = future.result(timeout=5)
        finally:
            future.cancel()
        assert frame['type'] == 'websocket.send'
        packet = json.loads(frame['text'])
        assert packet['type'] == 'alert' and packet['data']['id'] == alert
    elders = client.get('/api/v1/elder', headers=family_header)
    alerts = client.get('/api/v1/alerts', headers=family_header)
    assert elders.status_code == alerts.status_code == 200
    assert isinstance(elders.json(), list) and isinstance(alerts.json(), list)
    # Export only synthetic business payloads, never credential headers or tokens.
    env = {**os.environ,
           'ELDERGUARD_WS_MESSAGE': json.dumps(packet),
           'ELDERGUARD_WS_ALERTS': json.dumps(alerts.json()),
           'ELDERGUARD_WS_ELDERS': json.dumps(elders.json())}
    result = subprocess.run([
        'node', '--test', '--test-name-pattern=WS01/WS08/WS09/WS13',
        'tests/alert-socket.test.js',
    ], cwd=Path(__file__).resolve().parents[2] / 'family-miniapp',
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    print(result.stdout.encode('ascii', 'backslashreplace').decode('ascii'))
    assert result.returncode == 0, result.stdout + result.stderr
