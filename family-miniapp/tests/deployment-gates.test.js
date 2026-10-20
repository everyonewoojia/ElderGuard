'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { environment, flush } = require('./helpers/socket-runtime');

const invalidBases = [undefined, null, '', ' ', 'https://your-server.com/api/v1',
  'http://contract.invalid/api/v1', 'ws://contract.invalid/api/v1', 'javascript:alert(1)',
  'https://user:pass@contract.invalid/api/v1', 'https://contract.invalid@evil.invalid/api/v1',
  'https://contract.invalid.evil.invalid/api/v1', 'https://evil.invalid/api/v1',
  'https:///api/v1', 'https://contract.invalid:80/api/v1', 'https://contract.invalid:0443/api/v1',
  'https://contract.invalid/api/v2', 'https://contract.invalid/api/v1/../v1',
  'https://contract.invalid/api/v1?token=synthetic', 'https://contract.invalid/api/v1#fragment',
  'https://%63ontract.invalid/api/v1', 'https://contract.invalid\\@evil.invalid/api/v1',
  'https://contract.invalid/api%2fv1', 'https://contract.invalid/api/v1\n'];

for (const [index, baseUrl] of invalidBases.entries()) {
  test(`configuration ${index}: REST, login and socket reject before native calls`, async () => {
    const e = environment();
    e.app.globalData.baseUrl = baseUrl;
    const api = require('../utils/api');
    await assert.rejects(api.get('/alerts'), (error) => error.code === 'API_CONFIG_INVALID');
    const page = e.page('login');
    page.setData({ phone: 'synthetic-user', password: 'synthetic-password' });
    page.login();
    assert.equal(e.requests.length, 0);
    e.app._boundElders = [7]; e.app.globalData.currentElderId = 7;
    e.app.alertSocket.setForeground(true);
    e.app.alertSocket.connect(); e.app.alertSocket.sync();
    assert.equal(e.connections.length, 0);
    assert.equal(e.clock.jobs.size, 0);
    assert.notEqual(e.app.alertSocket.status().state, 'connecting');
  });
}

for (const hosts of [undefined, [], ['*'], ['*.invalid'], ['contract.invalid:443'], ['evil.invalid']]) {
  test(`explicit trusted host list required: ${JSON.stringify(hosts)}`, async () => {
    const e = environment(); e.app.globalData.apiTrustedHosts = hosts;
    await assert.rejects(require('../utils/api').get('/elder'));
    e.app._boundElders = [7]; e.app.globalData.currentElderId = 7;
    e.app.alertSocket.setForeground(true); e.app.alertSocket.connect();
    assert.equal(e.requests.length, 0); assert.equal(e.connections.length, 0);
    assert.equal(e.clock.jobs.size, 0);
  });
}

for (const suffix of ['', '/', ':443']) {
  test(`valid explicit host and canonical path preserve REST and WSS: ${suffix}`, async () => {
    const e = environment();
    e.app.globalData.baseUrl = suffix === ':443'
      ? 'https://contract.invalid:443/api/v1' : `https://contract.invalid/api/v1${suffix}`;
    await require('../utils/api').post('/tasks', { title: 'synthetic' });
    assert.equal(e.requests[0].url, 'https://contract.invalid/api/v1/tasks');
    assert.equal(e.requests[0].method, 'POST');
    assert.deepEqual(e.requests[0].data, { title: 'synthetic' });
    assert.equal(e.requests[0].header.Authorization, 'Bearer synthetic.token.value');
    e.app.onShow(); await flush();
    assert.equal(e.connections[0].url, 'wss://contract.invalid/api/v1/ws/alerts');
    assert.equal(e.connections[0].header.Authorization, 'Bearer synthetic.token.value');
    assert.ok(!e.connections[0].url.includes(e.app.globalData.token));
    assert.ok(!e.connections[0].url.includes('?')); e.app.onHide();
  });
}

for (const reason of ['flag', 'truthy flag', 'background', 'token', 'elder', 'unbound', 'configuration']) {
  test(`all connection entries fail closed: ${reason}`, async () => {
    const e = environment(); e.app.onShow(); await flush();
    const old = e.sockets[0];
    old.emit('Error');
    const before = e.connections.length;
    if (reason === 'flag') e.app.globalData.alertSocketHeaderVerified = false;
    if (reason === 'truthy flag') e.app.globalData.alertSocketHeaderVerified = 'true';
    if (reason === 'background') e.app.onHide();
    if (reason === 'token') e.app.globalData.token = '';
    if (reason === 'elder') e.app.globalData.currentElderId = null;
    if (reason === 'unbound') e.app.globalData.currentElderId = 99;
    if (reason === 'configuration') e.app.globalData.apiTrustedHosts = [];
    e.app.alertSocket.connect(); e.app.alertSocket.sync(); e.clock.tick(100000);
    assert.equal(e.connections.length, before); assert.equal(e.clock.jobs.size, 0);
    assert.notEqual(e.app.alertSocket.status().state, 'connecting');
    old.emit('Open'); old.emit('Close', { code: 1006 });
    assert.equal(e.clock.jobs.size, 0); e.app.onHide();
  });
}

test('flag false through direct entry, sync and foreground recovery never connects', async () => {
  const e = environment({ verified: false }); e.app.onShow(); await flush();
  e.app.alertSocket.connect(); e.app.alertSocket.sync();
  e.app.onHide(); e.app.onShow(); await flush(); e.clock.tick(100000);
  assert.equal(e.connections.length, 0); assert.equal(e.clock.jobs.size, 0);
});

test('closing gate retires an existing task and ignores all late callbacks', async () => {
  const e = environment(); e.app.onShow(); await flush();
  const old = e.sockets[0]; e.app.globalData.alertSocketHeaderVerified = false;
  e.app.alertSocket.connect();
  assert.equal(old.closes, 1); assert.equal(e.connections.length, 1);
  old.emit('Open'); old.emit('Error'); old.emit('Close', { code: 1006 });
  e.clock.tick(100000); assert.equal(e.clock.jobs.size, 0);
});

test('gate revocation observed by native callback immediately releases task and timers', async () => {
  const e = environment(); e.app.onShow(); await flush();
  const old = e.sockets[0]; e.app.globalData.alertSocketHeaderVerified = false;
  old.emit('Error');
  assert.equal(old.closes, 1); assert.equal(e.clock.jobs.size, 0);
  e.clock.tick(100000); assert.equal(e.connections.length, 1);
});

test('config errors never echo credentials or sensitive request objects', async () => {
  const e = environment(); e.app.globalData.baseUrl = 'https://synthetic.secret@contract.invalid/api/v1';
  await assert.rejects(require('../utils/api').post('/tasks', { private: 'synthetic.secret' }), (error) => {
    assert.equal(error.code, 'API_CONFIG_INVALID');
    assert.ok(!String(error).includes('synthetic.secret'));
    assert.ok(!JSON.stringify(error).includes('Authorization'));
    return true;
  });
  assert.equal(e.requests.length, 0);
});

test('public connect does not duplicate an active task or bypass retry exhaustion', async () => {
  const e = environment(); e.app.onShow(); await flush();
  e.app.alertSocket.connect(); assert.equal(e.connections.length, 1);
  for (let i = 0; i < 6; i++) { e.sockets.at(-1).emit('Error'); e.clock.tick(16000); }
  assert.equal(e.app.alertSocket.status().state, 'exhausted');
  const count = e.connections.length; e.app.alertSocket.connect();
  assert.equal(e.connections.length, count); e.app.onHide();
});

function loginEnvironment() {
  const e = environment({ token: '', verified: false });
  e.state.defer = true;
  e.storage = []; e.navigations = []; e.toasts = [];
  e.wx.setStorageSync = (key, value) => e.storage.push({ key, value });
  e.wx.switchTab = (options) => e.navigations.push(options);
  e.wx.showToast = (options) => e.toasts.push(options);
  e.pageLogin = () => { const page = e.page('login'); page.setData({ phone: 'synthetic-user', password: 'synthetic-password' }); return page; };
  return e;
}
function success(request, token = 'synthetic.accepted') {
  request.success({ statusCode: 200, data: { access_token: token } });
}

test('logout invalidates login; late success cannot store, refresh or navigate', () => {
  const e = loginEnvironment(); const page = e.pageLogin(); page.login();
  e.app.clearAlertSession(); success(e.pending[0]);
  assert.equal(e.app.globalData.token, ''); assert.equal(e.storage.length, 0);
  assert.equal(e.requests.length, 1); assert.equal(e.navigations.length, 0);
});

test('newer login wins across pages; late callbacks cannot change binding or navigation', async () => {
  const e = loginEnvironment(); const first = e.pageLogin(), second = e.pageLogin();
  first.login(); second.login(); success(e.pending[1], 'synthetic.new');
  assert.equal(e.app.globalData.token, 'synthetic.new');
  const binding = e.pending[2]; binding.success({ statusCode: 200, data: [{ id: 8 }] }); await flush();
  first.onUnload?.(); success(e.pending[0], 'synthetic.old');
  e.pending[0].fail({ errMsg: 'synthetic' });
  assert.equal(e.app.globalData.token, 'synthetic.new');
  assert.equal(e.app.globalData.currentElderId, 8);
  assert.equal(e.storage.length, 1); assert.equal(e.requests.length, 3);
  assert.equal(e.navigations.length, 1); assert.equal(e.toasts.length, 0);
});

test('latest pending attempt blocks an earlier success before it completes', () => {
  const e = loginEnvironment(); const p = e.pageLogin(); p.login(); p.login();
  success(e.pending[0]); assert.equal(e.storage.length, 0);
  success(e.pending[1]); assert.equal(e.storage.length, 1);
});

test('unload invalidates pending login, but cleanup after success preserves session', () => {
  const e = loginEnvironment(); const p = e.pageLogin(); p.login(); p.onUnload?.();
  success(e.pending[0]); assert.equal(e.storage.length, 0);
  const next = e.pageLogin(); next.login(); success(e.pending[1]); next.onUnload?.();
  assert.equal(e.app.globalData.token, 'synthetic.accepted'); assert.equal(e.storage.length, 1);
});

for (const action of ['unload', 'new invalid attempt']) {
  test(`temporary invalid config cannot revive old login after ${action}`, () => {
    const e = loginEnvironment(); const p = e.pageLogin(); p.login();
    e.app.globalData.apiTrustedHosts = [];
    if (action === 'unload') p.onUnload(); else p.login();
    e.app.globalData.apiTrustedHosts = ['contract.invalid'];
    success(e.pending[0]);
    assert.equal(e.storage.length, 0); assert.equal(e.navigations.length, 0);
    assert.equal(e.app.globalData.token, '');
  });
}

for (const action of ['revoke config', 'new attempt']) {
  test(`synchronous reset during login cannot bypass final session guard: ${action}`, () => {
    const e = loginEnvironment(); const p = e.pageLogin(); p.login();
    e.app.watchAlerts((event) => {
      if (event.type !== 'reset') return;
      if (action === 'revoke config') e.app.globalData.apiTrustedHosts = [];
      else require('../utils/api').beginLoginAttempt();
    });
    success(e.pending[0]);
    assert.equal(e.app.globalData.token, ''); assert.equal(e.storage.length, 0);
    assert.equal(e.navigations.length, 0); assert.equal(e.toasts.length, 0);
  });
}

for (const data of [null, {}, { access_token: '' }, { access_token: 42 }, { access_token: 'bad token' }]) {
  test(`invalid login response cannot mutate session: ${JSON.stringify(data)}`, () => {
    const e = loginEnvironment(); const p = e.pageLogin(); p.login();
    assert.doesNotThrow(() => e.pending[0].success({ statusCode: 200, data }));
    assert.equal(e.storage.length, 0); assert.equal(e.navigations.length, 0);
    assert.equal(e.app.globalData.token, ''); assert.equal(e.toasts.length, 1);
  });
}

for (const status of [401, 403]) {
  test(`login ${status} and network failure preserve feedback without session writes`, () => {
    const e = loginEnvironment(); const p = e.pageLogin(); p.login();
    e.pending[0].success({ statusCode: status, data: {} });
    assert.equal(e.toasts.at(-1).title, '登录失败');
    p.login(); e.pending[1].fail({ errMsg: 'synthetic' });
    assert.equal(e.toasts.at(-1).title, '网络错误');
    assert.equal(e.storage.length, 0); assert.equal(e.navigations.length, 0);
  });
}

test('legacy app login also validates configuration before wx.login or request', () => {
  const e = loginEnvironment(); e.app.globalData.apiTrustedHosts = [];
  let native = 0; e.wx.login = () => native++;
  e.app.login(); assert.equal(native, 0); assert.equal(e.requests.length, 0);
});

test('legacy app login shares attempt identity and logout invalidation', () => {
  const e = loginEnvironment(); let auth;
  e.wx.login = (options) => { auth = options; };
  let callbackCount = 0;
  e.app.login(() => callbackCount++); auth.success({ code: 'synthetic-code' });
  e.app.clearAlertSession();
  e.pending[0].success({ data: { code: 200, data: { token: 'synthetic.legacy' } } });
  assert.equal(e.storage.length, 0); assert.equal(e.app.globalData.token, '');
  assert.equal(callbackCount, 0);
});

test('legacy wx.login callback cannot start a request after logout or newer page login', () => {
  const e = loginEnvironment(); const callbacks = [];
  e.wx.login = (options) => callbacks.push(options);
  e.app.login(); e.app.clearAlertSession(); callbacks[0].success({ code: 'synthetic' });
  assert.equal(e.requests.length, 0);
  e.app.login(); e.pageLogin().login(); callbacks[1].success({ code: 'synthetic' });
  assert.equal(e.requests.length, 1);
});
