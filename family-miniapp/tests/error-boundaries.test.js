'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { environment, flush, message } = require('./helpers/socket-runtime');

// Synthetic canaries only; never load environment credentials or contact a server.
const privateText = 'Bearer synthetic.private.canary password=synthetic-secret SQL /private/internal';
function setup(options = {}) {
  const e = environment({ verified: false, ...options });
  e.state.defer = true; e.toasts = []; e.logs = [];
  e.wx.showToast = (value) => e.toasts.push(value);
  e.wx.showModal = (value) => e.toasts.push(value);
  e.api = require('../utils/api');
  return e;
}
async function capture(e, run) {
  const names = ['log', 'warn', 'error', 'info', 'debug'];
  const original = names.map((key) => console[key]);
  names.forEach((key) => { console[key] = (...args) => e.logs.push(args); });
  try { return await run(); }
  finally { names.forEach((key, i) => { console[key] = original[i]; }); }
}
function safe(e, error) {
  const output = JSON.stringify({ message: error?.message, error, toasts: e.toasts,
    logs: e.logs, diagnostics: e.app.alertSocket.status(), urls: [...e.requests, ...e.connections].map((r) => r.url) });
  for (const value of [privateText, 'synthetic.private.canary', 'synthetic-secret', '/private/internal', e.app.globalData.token]) {
    if (value) assert.ok(!output.includes(value), 'observable errors, diagnostics and URLs must exclude credentials');
  }
  assert.equal(e.logs.length, 0);
}
async function rejected(promise) {
  return promise.then(() => { assert.fail('failure must remain a rejected Promise'); }, (error) => error);
}
function begin(e, path = '/alerts') {
  const result = rejected(e.api.get(path));
  return { result, native: e.pending.at(-1) };
}

for (const [label, raw, code] of [
  ['timeout', { errMsg: `request:fail timeout ${privateText}`, header: { Authorization: privateText } }, 'NETWORK_TIMEOUT'],
  ['abort', { errMsg: `request:fail abort ${privateText}` }, 'NETWORK_CANCELLED'],
  ['connection', { errMsg: `request:fail connect fail ${privateText}` }, 'NETWORK_CONNECTION_FAILED'],
  ['unknown', { errMsg: privateText, request: { password: 'synthetic-secret' } }, 'NETWORK_FAILURE'],
  ['empty object', {}, 'NETWORK_FAILURE'], ['null', null, 'NETWORK_FAILURE'],
  ['undefined', undefined, 'NETWORK_FAILURE'], ['string', privateText, 'NETWORK_FAILURE'],
  ['array', [privateText], 'NETWORK_FAILURE'], ['number', 17, 'NETWORK_FAILURE'],
  ['wrong field type', { errMsg: { toString: () => privateText } }, 'NETWORK_FAILURE'],
]) {
  test(`network ${label}: controlled rejection, safe consumer feedback and diagnostics`, async () => {
    const e = setup(), token = e.app.globalData.token;
    await capture(e, async () => {
      const pending = begin(e); pending.native.fail(raw); const error = await pending.result;
      assert.ok(error instanceof Error); assert.notEqual(error, raw); assert.equal(error.code, code);
      assert.equal(error.errMsg, undefined); assert.equal(error.cause, undefined);
      assert.ok(e.app.globalData.token === token); safe(e, error);
    });
  });
}

test('synchronous native exception is normalized without exposing the thrown object', async () => {
  const e = setup();
  await capture(e, async () => {
    e.wx.request = () => { throw new Error(privateText); };
    const error = await rejected(e.api.get('/alerts'));
    assert.equal(error.code, 'NETWORK_FAILURE'); safe(e, error);
  });
});

for (const [label, body] of [
  ['internal message', { message: privateText, detail: privateText, stack: privateText }],
  ['HTML', `<html>${privateText}</html>`], ['invalid JSON', `{${privateText}`],
  ['null', null], ['undefined', undefined], ['array', [privateText]],
  ['unexpected message type', { message: { password: 'synthetic-secret' } }],
]) {
  test(`HTTP 500 ${label}: fixed error and status, no response text retained`, async () => {
    const e = setup();
    await capture(e, async () => {
      const pending = begin(e); pending.native.success({ statusCode: 500, data: body });
      const error = await pending.result;
      assert.equal(error.statusCode, 500); assert.equal(error.code, 'HTTP_ERROR');
      assert.equal(error.message, '服务暂时不可用'); safe(e, error);
    });
  });
}

for (const status of [400, 404, 429, 503, 599]) {
  test(`HTTP ${status}: status classification survives untrusted error text`, async () => {
    const e = setup();
    await capture(e, async () => {
      const pending = begin(e); pending.native.success({ statusCode: status, data: { message: privateText } });
      const error = await pending.result;
      assert.equal(error.code, 'HTTP_ERROR'); assert.equal(error.statusCode, status); safe(e, error);
    });
  });
}

for (const [label, response] of [['null', null], ['missing status', {}], ['string status', { statusCode: '500', data: privateText }]]) {
  test(`malformed native response ${label}: settles safely without an asynchronous throw`, async () => {
    const e = setup(), pending = begin(e);
    await capture(e, async () => {
      assert.doesNotThrow(() => pending.native.success(response));
      const error = await pending.result; assert.equal(error.code, 'HTTP_RESPONSE_INVALID'); safe(e, error);
    });
  });
}

for (const status of [401, 403]) {
  test(`HTTP ${status}: existing current-session auth semantics and safe text`, async () => {
    const e = setup(), token = e.app.globalData.token, version = e.app._bindingVersion;
    await capture(e, async () => {
      const pending = begin(e); pending.native.success({ statusCode: status, data: { message: privateText } });
      const error = await pending.result; assert.equal(error.statusCode, status);
      assert.ok(e.app.globalData.token === (status === 401 ? '' : token));
      assert.equal(e.app._bindingVersion, version + (status === 401 ? 1 : 0));
      assert.equal(e.wx.redirect?.url, status === 401 ? '/pages/login/login' : undefined); safe(e, error);
    });
  });
  test(`old ${status} with identical credential cannot clear the new session`, async () => {
    const e = setup(), pending = begin(e), token = e.app.globalData.token;
    e.app.clearAlertSession(); e.app.globalData.token = token;
    e.app._boundElders = [8]; e.app.selectAlertElder(8); const version = e.app._bindingVersion;
    await capture(e, async () => {
      pending.native.success({ statusCode: status, data: { message: privateText } });
      const error = await pending.result;
      assert.ok(e.app.globalData.token === token); assert.equal(e.app._bindingVersion, version);
      assert.equal(e.app.globalData.currentElderId, 8); safe(e, error);
    });
  });
}

test('invalid configuration fails before native I/O and is distinct from a network failure', async () => {
  const e = setup({ configured: false });
  e.app.globalData.baseUrl = `https://contract.invalid/api/v1?token=${privateText}`;
  await capture(e, async () => {
    const error = await rejected(e.api.get('/alerts'));
    assert.equal(error.code, 'API_CONFIG_INVALID'); assert.equal(e.requests.length, 0); safe(e, error);
  });
});

test('old configuration failure returns a safe context error instead of native details', async () => {
  const e = setup(), pending = begin(e);
  e.app.configureEnvironment('development', { environment: 'development', source: 'offline-test',
    baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'] });
  await capture(e, async () => {
    pending.native.fail({ errMsg: privateText }); const error = await pending.result;
    assert.equal(error.code, 'API_CONTEXT_CHANGED'); safe(e, error);
  });
});

test('normal arrays, objects and envelopes pass through by identity without normalization', async () => {
  const e = setup();
  for (const body of [[], [message().data], { id: 7 }, { code: 200, data: { list: [] } }]) {
    const result = e.api.get('/alerts'); e.pending.at(-1).success({ statusCode: 200, data: body });
    assert.strictEqual(await result, body);
  }
});

test('network failure retains loaded alerts, while a successful empty array clears them', async () => {
  const e = setup(); e.app._boundElders = [7]; e.app.selectAlertElder(7);
  const page = e.page('alerts'); page.onShow();
  e.pending.at(-1).success({ statusCode: 200, data: [message().data] }); await flush();
  await capture(e, async () => {
    page.loadAlerts(); e.pending.at(-1).fail({ errMsg: privateText }); await flush();
    assert.deepEqual(page.data.alerts, [message().data]);
    page.loadAlerts(); e.pending.at(-1).success({ statusCode: 200, data: [] }); await flush();
    assert.deepEqual(page.data.alerts, []); safe(e);
  });
});

test('old failed refresh finally cannot release a newer session refresh', async () => {
  const e = setup(); e.app._boundElders = [7]; e.app.selectAlertElder(7);
  const page = e.page('alerts'); page.onShow(); const old = e.pending.at(-1), token = e.app.globalData.token;
  e.app.clearAlertSession(); e.app.globalData.token = token; e.app._boundElders = [7]; e.app.selectAlertElder(7);
  page.onShow(); const current = page._alertRequest, native = e.pending.at(-1), count = e.requests.length;
  await capture(e, async () => {
    old.fail({ errMsg: privateText }); await flush();
    assert.strictEqual(page._alertRequest, current); assert.equal(e.requests.length, count);
    native.success({ statusCode: 200, data: [message(22).data] }); await flush();
    assert.deepEqual(page.data.alerts, [message(22).data]); safe(e);
  });
});

test('login failure ignores native and server details in user feedback', async () => {
  const e = setup(), page = e.page('login'); page.setData({ phone: 'synthetic-user', password: 'synthetic-password' });
  await capture(e, async () => {
    page.login(); e.pending.at(-1).fail({ errMsg: privateText });
    page.login(); e.pending.at(-1).success({ statusCode: 500, data: { message: privateText } });
    assert.deepEqual(e.toasts.map((v) => v.title), ['网络错误', '登录失败']); safe(e);
  });
});

for (const kind of ['error', '4401', 'cleanup_failed']) {
  test(`Socket ${kind}: diagnostics exclude native details and retain lifecycle safety`, async () => {
    // Explicit test-only override exercises a simulated platform; production stays false.
    const e = setup({ verified: true }); e.state.defer = false;
    await capture(e, async () => {
      e.app.onShow(); await flush(); const socket = e.sockets[0]; assert.ok(socket);
      if (kind === 'error') { socket.emit('Error', { errMsg: privateText }); assert.equal(e.app.alertSocket.status().state, 'reconnecting'); }
      if (kind === '4401') {
        socket.emit('Close', { code: 4401, reason: privateText });
        assert.equal(e.app.alertSocket.status().state, 'auth_failed'); assert.equal(e.clock.jobs.size, 0);
      }
      if (kind === 'cleanup_failed') {
        socket.failClose = true; e.app.onHide();
        assert.equal(e.app.alertSocket.status().state, 'cleanup_failed');
        e.app.onShow(); await flush(); assert.equal(e.connections.length, 1);
      }
      assert.deepEqual(Object.keys(e.app.alertSocket.status()).sort(), ['attempts', 'listeners', 'seen', 'state']);
      safe(e); e.app.onHide();
    });
  });
}
