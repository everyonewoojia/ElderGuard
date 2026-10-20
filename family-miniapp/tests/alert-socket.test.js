'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { environment, message, flush } = require('./helpers/socket-runtime');

async function start(options) {
  const env = environment(options);
  assert.ok(env.app.alertSocket, '应用缺少实时告警消费者及连接所有者');
  env.app.onShow();
  await flush();
  return env;
}

test('WS02/WS12: one current Bearer Header, WSS URL contains no token', async () => {
  const e = await start();
  assert.equal(e.connections.length, 1);
  assert.equal(e.connections[0].url, 'wss://contract.invalid/api/v1/ws/alerts');
  assert.equal(e.connections[0].header.Authorization, 'Bearer synthetic.token.value');
  assert.equal(e.app.alertSocket.status().state, 'connecting');
  e.sockets[0].emit('Open');
  assert.equal(e.app.alertSocket.status().state, 'connected');
  assert.ok(!JSON.stringify(e.app.alertSocket.status()).includes(e.app.globalData.token));
  e.app.onHide();
});

for (const [label, options] of [['missing token', { token: '' }], ['platform unverified', { verified: false }]]) {
  test(`WS02/WS12: no connection with ${label}; REST remains available`, async () => {
    const e = await start(options);
    assert.equal(e.connections.length, 0);
    if (options.token !== '') {
      e.state.alerts = [message().data];
      const page = e.page('alerts'); page.onShow(); await flush();
      assert.deepEqual(page.data.alerts, e.state.alerts);
      page.onUnload();
    }
    e.app.onHide();
  });
}

test('WS02: missing binding or insecure base URL does not connect', async () => {
  const e = environment();
  assert.ok(e.app.alertSocket, '应用缺少实时告警消费者');
  e.state.elders = [];
  e.app.onShow(); await flush();
  assert.equal(e.connections.length, 0);
  e.app.globalData.baseUrl = 'http://contract.invalid/api/v1';
  e.state.elders = [{ id: 7 }];
  await e.app.refreshAlertBinding();
  assert.equal(e.connections.length, 0);
  e.app.onHide();
});

test('WS04/WS16: repeated pages share one owner; hidden/unloaded pages unsubscribe', async () => {
  const e = await start();
  const index = e.page('index'), alerts = e.page('alerts');
  index.onLoad(); index.onShow(); alerts.onShow(); alerts.onShow(); await flush();
  assert.equal(e.connections.length, 1);
  assert.equal(e.app.alertSocket.status().listeners, 2);
  alerts.onUnload(); index.onHide();
  assert.equal(e.app.alertSocket.status().listeners, 0);
  const before = e.requests.length;
  e.sockets[0].emit('Message', { data: JSON.stringify(message()) }); await flush();
  assert.equal(e.requests.length, before);
  assert.equal(e.sockets[0].closes, 0);
  e.app.onHide();
  assert.equal(e.clock.jobs.size, 0);
});

test('WS05/WS16: background closes, late callbacks ignored, foreground restores once', async () => {
  const e = await start();
  const old = e.sockets[0];
  e.app.onHide();
  old.emit('Close', { code: 1006 }); old.emit('Open');
  e.clock.tick(100000);
  assert.equal(e.connections.length, 1);
  assert.equal(e.clock.jobs.size, 0);
  e.app.onShow(); e.app.onShow(); await flush();
  assert.equal(e.connections.length, 2);
  e.app.onHide();
});

test('WS07: network failure has one bounded exponential retry loop', async () => {
  const e = await start();
  for (let i = 0; i < 5; i++) {
    const socket = e.sockets.at(-1);
    socket.emit('Error', { errMsg: 'synthetic' }); socket.emit('Close', { code: 1006 });
    assert.equal(e.app.alertSocket.status().state, 'reconnecting');
    e.clock.tick(1000 * 2 ** i - 1);
    assert.equal(e.connections.length, i + 1);
    e.clock.tick(1);
    assert.equal(e.connections.length, i + 2);
  }
  e.sockets.at(-1).emit('Close', { code: 1006 });
  assert.equal(e.app.alertSocket.status().state, 'exhausted');
  e.clock.tick(100000);
  assert.equal(e.connections.length, 6);
  assert.equal(e.clock.jobs.size, 0);
  e.app.onHide();
});

test('WS07/WS11: successful reconnect resynchronizes REST and clears retry task', async () => {
  const e = await start();
  const page = e.page('alerts'); page.onShow(); await flush();
  e.sockets[0].emit('Close', { code: 1006 }); e.clock.tick(1000);
  e.state.alerts = [message().data];
  e.sockets[1].emit('Open'); await flush();
  assert.deepEqual(page.data.alerts, e.state.alerts);
  e.clock.tick(30000);
  assert.equal(e.clock.jobs.size, 0);
  assert.equal(e.connections.length, 2);
  page.onUnload(); e.app.onHide();
});

test('WS03/WS06: 4401 stops retries, clears login, ignores old messages', async () => {
  const e = await start();
  const page = e.page('alerts'); page.onShow(); await flush();
  const socket = e.sockets[0];
  socket.emit('Close', { code: 4401, reason: 'must not echo credentials' });
  assert.equal(e.app.globalData.token, '');
  assert.equal(e.wx.redirect.url, '/pages/login/login');
  assert.deepEqual(page.data.alerts, []);
  assert.equal(e.app.alertSocket.status().state, 'auth_failed');
  socket.emit('Message', { data: JSON.stringify(message()) }); socket.emit('Close', { code: 1006 });
  e.clock.tick(100000);
  assert.equal(e.connections.length, 1);
  assert.equal(e.clock.jobs.size, 0);
  page.onUnload();
});

test('WS06/WS09: elder and account changes isolate callbacks and dedup cache', async () => {
  const e = await start();
  let hits = 0;
  e.app.alertSocket.subscribe(() => hits++);
  e.sockets[0].emit('Message', { data: JSON.stringify(message()) });
  assert.equal(hits, 1);
  e.app.selectAlertElder(8);
  assert.equal(e.app.alertSocket.status().seen, 0);
  e.sockets[0].emit('Message', { data: JSON.stringify(message(22)) });
  assert.equal(hits, 1);
  assert.equal(e.app.alertSocket.status().listeners, 0);
  const old = e.sockets.at(-1);
  e.app.clearAlertSession();
  e.app.globalData.token = 'new.synthetic.token';
  await e.app.refreshAlertBinding();
  let newHits = 0;
  e.app.alertSocket.subscribe(() => newHits++);
  old.emit('Message', { data: JSON.stringify(message()) });
  e.sockets.at(-1).emit('Message', { data: JSON.stringify(message()) });
  assert.equal(newHits, 1);
  e.app.onHide();
});

test('WS01/WS08/WS09/WS13: real message refreshes bare REST arrays; duplicates stay singular', async () => {
  const e = await start();
  const page = e.page('alerts'); page.onShow(); await flush();
  const payload = process.env.ELDERGUARD_WS_MESSAGE ? JSON.parse(process.env.ELDERGUARD_WS_MESSAGE) : message();
  const rows = [payload.data, message(payload.data.id + 1, 8).data];
  // The Python integration supplies the matching actual REST JSON as well.
  e.state.alerts = process.env.ELDERGUARD_WS_ALERTS ? JSON.parse(process.env.ELDERGUARD_WS_ALERTS) : rows;
  e.sockets[0].emit('Message', { data: JSON.stringify(payload) }); await flush();
  const expected = e.state.alerts.filter((a) => a.elder_id === e.app.globalData.currentElderId);
  assert.ok(expected.length > 0);
  assert.deepEqual(page.data.alerts, expected);
  const before = e.requests.length;
  e.sockets[0].emit('Message', { data: JSON.stringify(payload) }); await flush();
  assert.equal(e.requests.length, before);
  assert.equal(new Set(page.data.alerts.map((a) => a.id)).size, page.data.alerts.length);
  page.onUnload(); e.app.onHide();
});

test('WS08: invalid JSON, unknown types, missing fields and other elders are rejected', async () => {
  const e = await start();
  let hits = 0; e.app.alertSocket.subscribe(() => hits++);
  const bad = ['{', 'null', '[]', JSON.stringify({ type: 'ping' }), JSON.stringify({ type: 'alert', data: { id: 1, elder_id: 7 } }), JSON.stringify(message(1, 99))];
  for (const key of Object.keys(message().data)) {
    const value = message(); delete value.data[key]; bad.push(JSON.stringify(value));
  }
  for (const [key, value] of [['id', -1], ['elder_id', '7'], ['priority', 'P9'], ['status', 'unknown'], ['created_at', 'bad'], ['content', {}], ['alert_type', 'unknown']]) {
    const payload = message(); payload.data[key] = value; bad.push(JSON.stringify(payload));
  }
  for (const data of bad) assert.doesNotThrow(() => e.sockets[0].emit('Message', { data }));
  assert.equal(hits, 0);
  assert.equal(e.app.alertSocket.status().seen, 0);
  e.app.onHide();
});

test('WS09: bounded 256-ID cache; reconnect replay cannot grow page rows', async () => {
  const e = await start();
  let hits = 0; e.app.alertSocket.subscribe(() => hits++);
  for (let id = 1; id <= 400; id++) e.sockets[0].emit('Message', { data: JSON.stringify(message(id)) });
  assert.equal(hits, 400);
  assert.equal(e.app.alertSocket.status().seen, 256);
  e.sockets[0].emit('Close', { code: 1006 }); e.clock.tick(1000);
  e.sockets[1].emit('Message', { data: JSON.stringify(message(400)) });
  assert.equal(hits, 400);
  e.app.onHide();
});

test('WS10/WS13: disconnected REST works; failure retains loaded data and empty success clears', async () => {
  const e = await start();
  const page = e.page('alerts');
  e.state.alerts = [message().data]; page.onShow(); await flush();
  e.sockets[0].emit('Close', { code: 1006 });
  e.state.failRest = true; page.loadAlerts(); await flush();
  assert.deepEqual(page.data.alerts, e.state.alerts);
  e.state.failRest = false; e.state.alerts = []; page.loadAlerts(); await flush();
  assert.deepEqual(page.data.alerts, []);
  page.onUnload(); e.app.onHide();
});

test('WS06/WS16: stale REST responses after logout or unload cannot mutate a page', async () => {
  const e = await start();
  e.state.defer = true;
  const page = e.page('alerts'); page.onShow();
  e.app.clearAlertSession(); page.onUnload();
  for (const request of e.pending) request.success({ statusCode: 200, data: [message().data] });
  await flush();
  assert.deepEqual(page.data.alerts, []);
  assert.equal(e.clock.jobs.size, 0);
});

for (const failure of ['throwConnect', 'immediateFail', 'timeout', 'throwClose', 'failClose']) {
  test(`WS04/WS16: ${failure} retains no duplicate timers or active callbacks`, async () => {
    const e = await start();
    if (failure === 'timeout') e.clock.tick(10000);
    else if (failure.endsWith('Close')) {
      e.sockets[0][failure] = true;
      e.sockets[0].emit('Error');
      assert.equal(e.clock.jobs.size, 0);
    } else {
      e.state[failure] = true;
      e.sockets[0].emit('Error'); e.clock.tick(1000);
      assert.equal(e.clock.jobs.size, 1);
    }
    e.app.onHide();
    for (const socket of e.sockets) { socket.emit('Close', { code: 1006 }); socket.emit('Open'); }
    e.clock.tick(100000);
    assert.equal(e.clock.jobs.size, 0);
  });
}

for (const status of [401, 403]) {
  test(`WS03: handshake transport error plus REST ${status} stops retry without a close frame`, async () => {
    const e = await start();
    e.state.httpStatus = status;
    e.sockets[0].emit('Error'); await flush();
    e.clock.tick(100000);
    assert.equal(e.app.globalData.token, '');
    assert.equal(e.connections.length, 1);
    assert.equal(e.clock.jobs.size, 0);
    assert.equal(e.wx.redirect.url, '/pages/login/login');
  });
}

test('WS06/WS13: late old REST 401 cannot log out the next session', async () => {
  const e = await start();
  e.state.defer = true;
  const api = require('../utils/api');
  const request = api.get('/alerts');
  e.app.clearAlertSession();
  e.app.globalData.token = 'next.synthetic.token';
  e.pending[0].success({ statusCode: 401, data: { message: 'synthetic old token expired' } });
  await assert.rejects(request, /未授权/);
  assert.equal(e.app.globalData.token, 'next.synthetic.token');
  assert.equal(e.wx.redirect, undefined);
  e.app.onHide();
});

test('WS06: late native close failure cannot change the next session state', async () => {
  const e = await start();
  const old = e.sockets[0];
  e.app.selectAlertElder(8);
  old.closeOptions.fail({ errMsg: 'synthetic late failure' });
  assert.equal(e.app.alertSocket.status().state, 'connecting');
  e.app.onHide();
});

test('WS06: index follows the selected elder and clears account data on logout', async () => {
  const e = await start();
  const page = e.page('index'); page.onLoad(); await flush();
  e.app.selectAlertElder(8); await flush();
  assert.equal(page.data.elderId, 8);
  e.app.clearAlertSession();
  assert.equal(page.data.elderId, null);
  assert.equal(page.data.elder, null);
  assert.deepEqual(page.data.tasks, []);
  page.onUnload();
});

test('WS09/WS16: in-flight REST coalesces signals and ignores an unloaded page', async () => {
  const e = await start();
  const page = e.page('alerts'); page.onShow(); await flush();
  e.state.defer = true;
  const before = e.requests.filter((r) => r.url.endsWith('/alerts')).length;
  for (let id = 30; id < 130; id++) e.sockets[0].emit('Message', { data: JSON.stringify(message(id)) });
  assert.equal(e.requests.filter((r) => r.url.endsWith('/alerts')).length, before + 1);
  const request = e.pending[0];
  e.state.defer = false; e.state.alerts = [message(31).data];
  request.success({ statusCode: 200, data: [message(30).data] }); await flush();
  assert.equal(e.requests.filter((r) => r.url.endsWith('/alerts')).length, before + 2);
  assert.deepEqual(page.data.alerts, e.state.alerts);
  page.onUnload(); e.app.onHide();
});

test('WS12: native errors containing credentials are never logged or exposed in diagnostics', async () => {
  const e = await start();
  const logs = [];
  const old = [console.log, console.warn, console.error];
  console.log = console.warn = console.error = (...args) => logs.push(args);
  try {
    e.sockets[0].emit('Error', { errMsg: e.app.globalData.token });
    e.clock.tick(1000);
    e.sockets.at(-1).emit('Message', { data: e.app.globalData.token });
    assert.equal(logs.length, 0);
    assert.ok(!JSON.stringify(e.app.alertSocket.status()).includes(e.app.globalData.token));
  } finally {
    [console.log, console.warn, console.error] = old;
    e.app.onHide();
  }
});

test('WS03/WS07: short successful opens do not erase the retry limit', async () => {
  const e = await start();
  for (let attempt = 0; attempt < 6; attempt++) {
    e.sockets.at(-1).emit('Open');
    e.sockets.at(-1).emit('Close', { code: 1006 });
    e.clock.tick(16000);
  }
  assert.equal(e.connections.length, 6);
  assert.equal(e.app.alertSocket.status().state, 'exhausted');
  e.app.onHide();
});

test('WS16: a throwing page reset callback cannot prevent other cleanup', async () => {
  const e = await start();
  e.app.watchAlerts((event) => { if (event.type === 'reset') throw new Error('synthetic page error'); });
  const page = e.page('alerts'); page.onShow(); await flush();
  assert.doesNotThrow(() => e.app.clearAlertSession());
  assert.equal(e.app._alertWatches.size, 0);
  assert.equal(e.app.alertSocket.status().listeners, 0);
  assert.equal(e.clock.jobs.size, 0);
  page.onUnload();
});

test('WS06/WS16: a late close failure after synchronous connect failure is session isolated', async () => {
  const e = await start();
  e.state.immediateFail = true;
  e.sockets[0].emit('Error'); e.clock.tick(1000);
  const failed = e.sockets.at(-1);
  e.state.immediateFail = false;
  e.app.clearAlertSession();
  e.app.globalData.token = 'next.synthetic.token';
  await e.app.refreshAlertBinding();
  failed.closeOptions.fail({ errMsg: 'synthetic late native error' });
  assert.equal(e.app.alertSocket.status().state, 'connecting');
  e.app.onHide();
});
