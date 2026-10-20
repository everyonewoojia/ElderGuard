'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { environment, flush, message } = require('./helpers/socket-runtime');

function config(overrides = {}) {
  return { environment: 'test', source: 'offline-test', baseUrl: 'https://contract.invalid/api/v1',
    apiTrustedHosts: ['contract.invalid'], ...overrides };
}
function configure(e, expected = 'test', value = config()) {
  assert.equal(typeof e.app.configureEnvironment, 'function', 'missing explicit configuration entry');
  return e.app.configureEnvironment(expected, value);
}
function loginPage(e) {
  const page = e.page('login'); page.setData({ phone: 'synthetic-user', password: 'synthetic-password' }); return page;
}

test('production defaults are empty, stay isolated after test injection, and fail closed', async () => {
  const e = environment({ configured: false });
  assert.equal(e.app.globalData.baseUrl, '');
  assert.deepEqual(e.app.globalData.apiTrustedHosts, []);
  assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
  await assert.rejects(require('../utils/api').get('/alerts'));
  loginPage(e).login(); e.app.alertSocket.setForeground(true); e.app.alertSocket.connect();
  assert.equal(e.requests.length, 0); assert.equal(e.connections.length, 0); assert.equal(e.clock.jobs.size, 0);
  configure(e); await require('../utils/api').get('/alerts');
  const fresh = environment({ configured: false });
  assert.equal(fresh.app.globalData.baseUrl, ''); assert.deepEqual(fresh.app.globalData.apiTrustedHosts, []);
  await assert.rejects(require('../utils/api').get('/alerts')); assert.equal(fresh.requests.length, 0);
});

const invalid = [
  ['missing config', 'test', null], ['missing environment', '', config()],
  ['unknown environment', 'staging', config({ environment: 'staging' })],
  ['mixed environment', 'production', config()],
  ['unknown source', 'test', config({ source: 'automatic' })],
  ['missing source', 'test', config({ source: '' })],
  ['missing host list', 'test', config({ apiTrustedHosts: undefined })],
  ['host mismatch', 'test', config({ apiTrustedHosts: ['other.invalid'] })],
  ['wildcard', 'test', config({ apiTrustedHosts: ['*.invalid'] })],
  ['user info', 'test', config({ baseUrl: 'https://synthetic:secret@contract.invalid/api/v1' })],
  ['HTTP', 'test', config({ baseUrl: 'http://contract.invalid/api/v1' })],
  ['query', 'test', config({ baseUrl: 'https://contract.invalid/api/v1?token=synthetic' })],
  ['fragment', 'test', config({ baseUrl: 'https://contract.invalid/api/v1#synthetic' })],
  ['socket switch in config', 'test', config({ alertSocketHeaderVerified: true })],
  ['credentials in config', 'test', config({ token: 'synthetic.private' })],
  ['production offline source', 'production', config({ environment: 'production' })],
];
for (const [label, expected, value] of invalid) {
  test(`invalid ${label}: revoke configuration before any native network`, async () => {
    const e = environment(); configure(e);
    assert.throws(() => configure(e, expected, value), (error) => error.code === 'API_CONFIG_INVALID');
    await assert.rejects(require('../utils/api').get('/alerts'));
    loginPage(e).login(); let nativeLogin = 0; e.wx.login = () => nativeLogin++; e.app.login();
    e.app.alertSocket.setForeground(true); e.app.alertSocket.connect(); e.app.alertSocket.sync();
    e.clock.tick(100000);
    assert.equal(e.requests.length, 0); assert.equal(nativeLogin, 0); assert.equal(e.connections.length, 0);
    assert.equal(e.clock.jobs.size, 0); assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
  });
}

for (const host of ['localhost', '127.0.0.1', 'contract.invalid', 'contract.test', 'contract.example', 'example.com']) {
  test(`production cannot trust reserved, local or synthetic host: ${host}`, () => {
    const e = environment();
    assert.equal(typeof e.app.configureEnvironment, 'function', 'missing explicit configuration entry');
    assert.throws(() => configure(e, 'production', config({ environment: 'production', source: 'reviewed-config',
      baseUrl: `https://${host}/api/v1`, apiTrustedHosts: [host] })));
    assert.equal(e.requests.length, 0); assert.equal(e.connections.length, 0);
  });
}

test('one explicit config drives REST and form login while all socket entries remain closed', async () => {
  const e = environment(); const value = config(); configure(e, 'test', value);
  value.baseUrl = 'https://other.invalid/api/v1'; value.apiTrustedHosts[0] = 'other.invalid';
  e.app.globalData.token = 'synthetic.session';
  e.state.alerts = [message().data];
  const api = require('../utils/api'); assert.deepEqual(await api.get('/alerts'), e.state.alerts);
  assert.equal(e.requests[0].url, 'https://contract.invalid/api/v1/alerts');
  assert.equal(e.requests[0].header.Authorization, 'Bearer synthetic.session');
  e.state.defer = true; const page = loginPage(e); page.login();
  const request = e.pending[0];
  assert.equal(request.url, 'https://contract.invalid/api/v1/auth/login');
  assert.equal(request.method, 'POST');
  assert.equal(request.header['content-type'], 'application/x-www-form-urlencoded');
  request.success({ statusCode: 200, data: { access_token: 'synthetic.login' } });
  e.pending[1].success({ statusCode: 200, data: [{ id: 7 }] }); await flush();
  e.app.onShow(); e.app.alertSocket.connect(); e.app.alertSocket.sync();
  e.app.onHide(); e.app.onShow(); e.clock.tick(100000);
  assert.equal(e.connections.length, 0); assert.equal(e.clock.jobs.size, 0);
  assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
  e.app.onHide();
});

test('legacy app login uses same explicit source and can complete without opening socket', () => {
  const e = environment(); configure(e); e.state.defer = true;
  e.wx.login = (options) => options.success({ code: 'synthetic-code' });
  let result; e.app.login((value) => { result = value; });
  assert.equal(e.pending[0].url, 'https://contract.invalid/api/v1/auth/login');
  e.pending[0].success({ statusCode: 200, data: { code: 200, data: { token: 'synthetic.legacy' } } });
  assert.equal(result, true); assert.equal(e.app.globalData.token, 'synthetic.legacy');
  assert.equal(e.connections.length, 0);
});

for (const field of ['baseUrl', 'apiTrustedHosts', 'apiEnvironment', 'apiConfigSource']) {
  test(`direct mutable mirror change cannot bypass active configuration: ${field}`, async () => {
    const e = environment(); configure(e);
    const values = { baseUrl: 'https://other.invalid/api/v1', apiTrustedHosts: ['contract.invalid', 'other.invalid'],
      apiEnvironment: 'production', apiConfigSource: 'unknown' };
    e.app.globalData[field] = values[field];
    await assert.rejects(require('../utils/api').get('/alerts'));
    loginPage(e).login(); assert.equal(e.requests.length, 0);
  });
}

test('configuration switch closes old task, clears session, blocks late data and login', async () => {
  const e = environment(); e.app.onShow(); await flush();
  const old = e.sockets[0]; assert.ok(old); e.state.defer = true;
  const page = e.page('alerts'); page.onShow();
  const api = require('../utils/api'); const promise = api.get('/alerts');
  const rejected = assert.rejects(promise, (error) => error.code === 'API_CONTEXT_CHANGED');
  const pending = [...e.pending]; const p = loginPage(e); p.login(); const pendingLogin = e.pending.at(-1);
  configure(e, 'development', config({ environment: 'development' }));
  assert.equal(old.closes, 1); assert.equal(e.clock.jobs.size, 0); assert.equal(e.app.globalData.token, '');
  assert.equal(e.app.globalData.currentElderId, null); assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
  e.app.globalData.token = 'synthetic.token.value'; // Same JWT cannot revive old configuration epoch.
  for (const request of pending) request.success({ statusCode: 200, data: [message().data] });
  pendingLogin.success({ statusCode: 200, data: { access_token: 'synthetic.old' } }); await rejected; await flush();
  old.emit('Open'); old.emit('Message', { data: JSON.stringify(message()) }); old.emit('Close', { code: 4401 });
  assert.equal(e.app.globalData.token, 'synthetic.token.value'); assert.deepEqual(page.data.alerts, []);
  assert.equal(e.connections.length, 1); assert.equal(e.clock.jobs.size, 0); page.onUnload(); e.app.onHide();
});

for (const status of [401, 403]) {
  test(`late old-environment ${status} cannot log out the new session`, async () => {
    const e = environment({ verified: false }); e.state.defer = true;
    const promise = require('../utils/api').get('/elder');
    const rejected = assert.rejects(promise, (error) => error.code === 'API_CONTEXT_CHANGED');
    configure(e, 'development', config({ environment: 'development' }));
    e.app.globalData.token = 'synthetic.token.value';
    e.pending[0].success({ statusCode: status, data: { message: 'synthetic' } }); await rejected;
    assert.equal(e.app.globalData.token, 'synthetic.token.value'); assert.equal(e.wx.redirect, undefined);
  });
}

test('network failure remains failure with valid explicit configuration', async () => {
  const e = environment({ verified: false }); configure(e); e.state.failRest = true;
  await assert.rejects(require('../utils/api').get('/alerts'));
  assert.equal(e.connections.length, 0);
});

test('network failure output is controlled and does not retain native errMsg', async () => {
  const e = environment({ verified: false }); configure(e); e.state.failRest = true;
  await assert.rejects(require('../utils/api').get('/alerts'), (error) => {
    assert.ok(error instanceof Error);
    assert.equal(error.code, 'NETWORK_FAILURE');
    assert.equal(error.message, '网络请求失败，请重试');
    assert.equal(error.errMsg, undefined);
    assert.equal(error.cause, undefined);
    return true;
  });
});

test('placeholder hosts cannot hide as unused additional trusted entries', () => {
  const e = environment();
  assert.throws(() => configure(e, 'test', config({ apiTrustedHosts: ['contract.invalid', 'example.com'] })),
    (error) => error.code === 'API_CONFIG_INVALID');
  assert.equal(e.requests.length, 0);
});

test('reapplying the same configuration still invalidates old REST callbacks', async () => {
  const e = environment({ verified: false }); e.state.defer = true;
  const promise = require('../utils/api').get('/alerts');
  const rejected = assert.rejects(promise, (error) => error.code === 'API_CONTEXT_CHANGED');
  configure(e); e.pending[0].success({ statusCode: 200, data: [message().data] }); await rejected;
  assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
});

test('invalid replacement closes an existing task and leaves no retry or trusted config', async () => {
  const e = environment(); e.app.onShow(); await flush();
  const old = e.sockets[0]; old.emit('Error');
  assert.throws(() => configure(e, 'production', config()));
  old.emit('Open'); old.emit('Close', { code: 1006 }); e.clock.tick(100000);
  await assert.rejects(require('../utils/api').get('/alerts'));
  assert.equal(e.connections.length, 1); assert.equal(e.clock.jobs.size, 0);
  assert.equal(e.app.globalData.token, ''); assert.equal(e.app.globalData.baseUrl, '');
});

test('a reentrant configuration change cannot be overwritten by the outer operation', () => {
  const e = environment();
  e.app.watchAlerts((event) => {
    if (event.type === 'reset') {
      // Remove this callback before the nested lifecycle cleanup.
      e.app._alertWatches.clear();
      configure(e, 'development', config({ environment: 'development' }));
    }
  });
  assert.throws(() => configure(e));
  assert.equal(require('../utils/api').getConfig().environment, 'development');
  assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
});

test('stored token must be scoped to exact environment and trusted target', async () => {
  const e = environment({ configured: false });
  assert.equal(typeof e.app.configureEnvironment, 'function', 'missing explicit configuration entry');
  e.app.deploymentEnvironment = 'test'; e.app.deploymentConfig = config();
  let stored;
  e.wx.setStorageSync = (key, value) => { if (key === 'token') stored = value; };
  e.wx.getStorageSync = () => stored;
  e.app.onLaunch(); e.state.defer = true; loginPage(e).login();
  e.pending[0].success({ statusCode: 200, data: { access_token: 'synthetic.persisted' } });
  assert.equal(typeof stored, 'object'); assert.equal(stored.value, 'synthetic.persisted');
  e.app.onLaunch(); assert.equal(e.app.globalData.token, 'synthetic.persisted');
  e.app.deploymentEnvironment = 'development'; e.app.deploymentConfig = config({ environment: 'development' });
  e.app.onLaunch(); assert.equal(e.app.globalData.token, '');
  e.wx.getStorageSync = () => 'synthetic.unscoped';
  e.app.onLaunch(); assert.equal(e.app.globalData.token, '');
});
