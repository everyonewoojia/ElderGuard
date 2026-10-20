'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { environment, flush } = require('./helpers/socket-runtime');

function setup() {
  const e = environment({ verified: false }); e.state.defer = true;
  e.removals = 0; e.redirects = [];
  e.wx.removeStorageSync = () => e.removals++;
  e.wx.redirectTo = (options) => e.redirects.push(options.url);
  e.app._boundElders = [7]; e.app.selectAlertElder(7);
  return e;
}
function start(e) {
  const count = e.pending.length; e.app.checkAlertSession();
  assert.equal(e.pending.length, count + 1, 'one native REST request per current probe');
  assert.ok(e.app._authProbe?.promise);
  return { handle: e.app._authProbe, request: e.pending.at(-1) };
}
function finish(probe, result) {
  if (result === 'network') probe.request.fail({ errMsg: 'synthetic offline' });
  else if (result === 'reject401' || result === 'reject403') {
    const error = new Error('synthetic rejected request');
    error.statusCode = result === 'reject401' ? 401 : 403;
    probe.request.fail(error);
  } else probe.request.success({ statusCode: result, data: result === 200 ? [{ id: 7 }] : { message: 'synthetic failure' } });
}
async function login(e, token) {
  const page = e.page('login'); page.setData({ phone: 'synthetic-user', password: 'synthetic-password' });
  page.login(); e.pending.at(-1).success({ statusCode: 200, data: { access_token: token } });
  e.pending.at(-1).success({ statusCode: 200, data: [{ id: 8 }] }); await flush();
  assert.ok(e.app.globalData.token === token); assert.equal(e.app.globalData.currentElderId, 8);
}
function snapshot(e) {
  return { session: e.app._bindingVersion, login: e.app._loginVersion, config: e.app._configEpoch,
    elder: e.app.globalData.currentElderId, elders: [...e.app._boundElders],
    socket: e.app.alertSocket.status(), removals: e.removals, redirects: [...e.redirects] };
}
function unchanged(e, state, token) {
  assert.ok(e.app.globalData.token === token, 'current credential must be preserved');
  assert.deepEqual(snapshot(e), state); assert.equal(e.connections.length, 0);
}

for (const result of [401, 403, 500, 'network', 'reject401', 'reject403']) {
  test(`A01/A03: old ${result} cannot clear real new login with identical token text`, async () => {
    const e = setup(), token = e.app.globalData.token, old = start(e);
    const version = e.app._bindingVersion; e.app.clearAlertSession(); await login(e, token);
    assert.ok(e.app._bindingVersion > version);
    const state = snapshot(e); finish(old, result); await old.handle.promise;
    unchanged(e, state, token); assert.equal(e.app._authProbe, null);
  });
}

test('A02: late old success does not replace new binding, state or current pending probe', async () => {
  const e = setup(), token = e.app.globalData.token, old = start(e);
  e.app.clearAlertSession(); await login(e, token); const current = start(e), state = snapshot(e);
  finish(old, 200); await old.handle.promise;
  unchanged(e, state, token); assert.equal(e.app._authProbe, current.handle);
  finish(current, 200); await current.handle.promise;
  unchanged(e, state, token); assert.equal(e.app._authProbe, null);
});

test('A03: synchronous request exception belongs to its original session', async () => {
  const e = setup(), token = e.app.globalData.token, original = e.wx.request;
  e.wx.request = () => { const error = new Error('synthetic transport exception'); error.statusCode = 403; throw error; };
  e.app.checkAlertSession(); const old = e.app._authProbe;
  e.wx.request = original; e.app.clearAlertSession();
  const page = e.page('login'); page.setData({ phone: 'synthetic-user', password: 'synthetic-password' });
  page.login(); e.pending.at(-1).success({ statusCode: 200, data: { access_token: token } });
  e.pending.at(-1).success({ statusCode: 200, data: [{ id: 8 }] });
  const version = e.app._bindingVersion; await old.promise; await flush();
  assert.ok(e.app.globalData.token === token); assert.equal(e.app._bindingVersion, version);
  assert.equal(e.app.globalData.currentElderId, 8); assert.deepEqual(e.app._boundElders, [8]);
  assert.equal(e.redirects.length, 0); assert.equal(e.connections.length, 0);
});

for (const error of [null, undefined, 'synthetic offline']) {
  test(`A03: nonstandard rejection ${String(error)} is safely settled without expiring authentication`, async () => {
    const e = setup(), token = e.app.globalData.token, state = snapshot(e);
    e.wx.request = (options) => options.fail(error);
    e.app.checkAlertSession(); const probe = e.app._authProbe;
    await assert.doesNotReject(probe.promise);
    unchanged(e, state, token); assert.equal(e.app._authProbe, null);
  });
}

for (const result of [401, 403, 'reject401', 'reject403']) {
  test(`A04: current-session ${result} still expires authentication exactly once`, async () => {
    const e = setup(), probe = start(e), version = e.app._bindingVersion;
    finish(probe, result); await probe.handle.promise;
    assert.ok(e.app.globalData.token === ''); assert.equal(e.app.globalData.currentElderId, null);
    assert.deepEqual(e.app._boundElders, []); assert.equal(e.app._bindingVersion, version + 1);
    assert.deepEqual(e.redirects, ['/pages/login/login']); assert.equal(e.removals, 1);
    assert.equal(e.app.alertSocket.status().state, 'auth_failed'); assert.equal(e.app._authProbe, null);
    assert.equal(e.clock.jobs.size, 0); assert.equal(e.connections.length, 0);
  });
}

for (const result of [200, 500, 'network']) {
  test(`A04/A08: current ${result} preserves existing non-authentication semantics and permits next probe`, async () => {
    const e = setup(), token = e.app.globalData.token, state = snapshot(e), probe = start(e);
    finish(probe, result); await probe.handle.promise; unchanged(e, state, token);
    assert.equal(e.app._authProbe, null);
    const next = start(e); finish(next, 200); await next.handle.promise;
  });
}

for (const result of [200, 401, 403, 'network']) {
  test(`A05: old configuration ${result} cannot change the new configuration or its session`, async () => {
    const e = setup(), token = e.app.globalData.token, old = start(e);
    e.app.configureEnvironment('development', { environment: 'development', source: 'offline-test',
      baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'] });
    await login(e, token); const current = start(e), state = snapshot(e);
    finish(old, result); await old.handle.promise;
    unchanged(e, state, token); assert.equal(e.app.globalData.apiEnvironment, 'development');
    assert.equal(e.app._authProbe, current.handle); assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
    finish(current, 200); await current.handle.promise;
  });
}

test('A06: duplicate current-session calls coalesce until completion', async () => {
  const e = setup(), first = start(e), count = e.requests.length;
  for (let i = 0; i < 20; i++) e.app.checkAlertSession();
  assert.equal(e.requests.length, count); assert.equal(e.app._authProbe, first.handle);
  finish(first, 'network'); await first.handle.promise;
  const next = start(e); finish(next, 200); await next.handle.promise;
  assert.equal(e.requests.length, count + 1);
});

for (const order of ['old-first', 'new-first']) {
  for (const result of [401, 403, 200, 'network']) {
    test(`A06: ${order} completion of old ${result} cannot overwrite a newer successful session`, async () => {
      const e = setup(), token = e.app.globalData.token, old = start(e);
      e.app.clearAlertSession(); await login(e, token); const current = start(e), state = snapshot(e);
      if (order === 'old-first') {
        finish(old, result); await old.handle.promise;
        assert.equal(e.app._authProbe, current.handle);
        const count = e.requests.length; e.app.checkAlertSession(); assert.equal(e.requests.length, count);
        finish(current, 200); await current.handle.promise;
      } else {
        finish(current, 200); await current.handle.promise; finish(old, result); await old.handle.promise;
      }
      unchanged(e, state, token); assert.equal(e.app._authProbe, null);
    });
  }
}

test('A06: old finally cannot release a third probe started after the second completed', async () => {
  const e = setup(), token = e.app.globalData.token, old = start(e);
  e.app.clearAlertSession(); await login(e, token);
  const second = start(e); finish(second, 200); await second.handle.promise;
  const third = start(e), state = snapshot(e); finish(old, 403); await old.handle.promise;
  unchanged(e, state, token); assert.equal(e.app._authProbe, third.handle);
  finish(third, 200); await third.handle.promise;
});

for (const result of [200, 401, 403, 'network']) {
  test(`A07: old ${result} after logout cannot restore or clean the logged-out session again`, async () => {
    const e = setup(), old = start(e); e.app.clearAlertSession(); const state = snapshot(e);
    finish(old, result); await old.handle.promise; unchanged(e, state, '');
    const count = e.requests.length; e.app.checkAlertSession(); assert.equal(e.requests.length, count);
  });
}

test('A08: probe and foreground/background lifecycle do not enable production sockets', async () => {
  const e = setup(), probe = start(e), token = e.app.globalData.token;
  e.app.onShow(); e.app.onHide(); e.app.onShow();
  const state = snapshot(e); finish(probe, 200); await probe.handle.promise;
  unchanged(e, state, token); assert.equal(e.app.globalData.alertSocketHeaderVerified, false);
  assert.equal(e.clock.jobs.size, 0); e.app.onHide();
});

test('A08: failed closed configuration does not send a probe or expire an unrelated session', async () => {
  const e = setup(), token = e.app.globalData.token; e.app.globalData.apiTrustedHosts = [];
  const count = e.requests.length, state = snapshot(e); e.app.checkAlertSession();
  const probe = e.app._authProbe; await probe.promise;
  assert.equal(e.requests.length, count); unchanged(e, state, token); assert.equal(e.app._authProbe, null);
});
