"""Handshake credential ambiguity must fail closed in the real WS route."""
import pytest
from httpx import Headers
from starlette.websockets import WebSocketDisconnect

from app.services.ws_manager import manager


@pytest.fixture
def handshake(auth):
    """Create a synthetic family and a replayable alert in the protected database."""
    user = auth['register']('header-family', '13800007771')
    elder = auth['create_elder']('header-family', 'synthetic')
    alert = auth['create_alert'](elder)
    return auth['client'], auth['login']('header-family'), user['id'], alert


@pytest.mark.parametrize('mode', ['header', 'query', 'both', 'lowercase'])
def test_valid_handshake_credentials(handshake, mode):
    """Support Bearer without a URL credential, keeping compatible legacy use."""
    client, token, owner, alert = handshake
    headers = {'Authorization': f'{"bearer" if mode == "lowercase" else "Bearer"} {token}'} if mode != 'query' else {}
    url = '/api/v1/ws/alerts' + (f'?token={token}' if mode in ('query', 'both') else '')
    with client.websocket_connect(url, headers=headers) as socket:
        assert socket.receive_json()['data']['id'] == alert
    assert owner not in manager.online_users()


@pytest.mark.parametrize('case', [
    'empty', 'missing', 'basic', 'extra-space', 'trailing', 'comma',
    'invalid', 'conflict', 'duplicate-header', 'duplicate-query', 'empty-query',
])
def test_ambiguous_or_invalid_credentials_never_fall_back(handshake, case, caplog):
    """A valid legacy token must not rescue an invalid or ambiguous handshake."""
    client, token, owner, _ = handshake
    values = {
        'empty': '', 'missing': 'Bearer', 'basic': f'Basic {token}',
        'extra-space': f'Bearer  {token}', 'trailing': f'Bearer {token} ',
        'comma': f'Bearer {token}, Bearer {token}', 'invalid': 'Bearer synthetic-invalid',
        'conflict': 'Bearer synthetic-conflict',
    }
    url = f'/api/v1/ws/alerts?token={token}'
    headers = [('Authorization', values.get(case, f'Bearer {token}'))]
    if case == 'duplicate-header':
        headers.append(('authorization', f'Bearer {token}'))
    if case == 'duplicate-query':
        url += f'&token={token}'
    if case == 'empty-query':
        url = '/api/v1/ws/alerts?token='
    with pytest.raises(WebSocketDisconnect) as failure:
        with client.websocket_connect(url, headers=Headers(headers)):
            pytest.fail('Ambiguous credentials were accepted')
    assert failure.value.code == 4401
    assert owner not in manager.online_users()
    assert token not in caplog.text
