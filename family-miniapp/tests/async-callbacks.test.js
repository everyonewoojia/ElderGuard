'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { environment, flush, message } = require('./helpers/socket-runtime');

const elder = (name = 'synthetic current', id = 7) => ({ id, name, privacy_enabled: false });
const task = (id) => ({ id, elder_id: 7, title: 'synthetic task', task_type: 'medication', is_active: true });
function setup(entry) {
  const e = environment({ verified: false }); e.state.defer = true; e.toasts = [];
  e.wx.showToast = (options) => e.toasts.push(options.title);
  e.app._boundElders = [7, 8]; e.app.selectAlertElder(7);
  function page(route) {
    let config; global.Page = (value) => { config = value; };
    const file = path.join(__dirname, '../pages', `${route}.js`); delete require.cache[file]; require(file);
    const result = { ...config, data: structuredClone(config.data), writes: [] };
    result.setData = (patch) => { result.writes.push(structuredClone(patch)); Object.assign(result.data, patch); };
    return result;
  }
  e.pageEntry = page; e.view = page(entry);
  if (entry.startsWith('elder/') || entry.startsWith('tasks/')) e.view.onLoad({}); else e.view.onShow();
  return e;
}
function requests(e, route, method = 'GET') {
  return e.pending.filter((request) => request.url.endsWith(`/api/v1${route}`) && request.method === method);
}
function success(request, data) { request.success({ statusCode: 200, data }); }
function finish(request, outcome, data = {}) {
  if (outcome === 'network') request.fail({ errMsg: 'synthetic offline' });
  else request.success({ statusCode: outcome, data: outcome === 200 ? data : { message: 'synthetic failure' } });
}
async function login(e, selected = 7) {
  const token = e.app.globalData.token; e.app.clearAlertSession();
  const page = e.page('login'); page.setData({ phone: 'synthetic-user', password: 'synthetic-password' });
  page.login(); success(e.pending.at(-1), { access_token: token });
  success(e.pending.at(-1), [elder('new binding', selected)]); await flush();
  assert.ok(e.app.globalData.token === token); return token;
}

test('E01: newer home elder result wins over late populated or empty older result', async () => {
  for (const oldRows of [[elder('old')], []]) {
    const e = setup('index/index'), old = requests(e, '/elder')[0]; e.view.loadBindElder();
    success(requests(e, '/elder').at(-1), [elder('new')]); await flush();
    success(old, oldRows); await flush(); assert.equal(e.view.data.elder.name, 'new');
  }
});

test('E01: repeated home task requests cannot overwrite the newest list', async () => {
  const e = setup('index/index'); e.view.loadTasks(7); const old = requests(e, '/tasks').at(-1);
  e.view.loadTasks(7); success(requests(e, '/tasks').at(-1), [task(22)]); await flush();
  success(old, [task(11)]); await flush(); assert.deepEqual(e.view.data.tasks, [task(22)]);
});

for (const route of ['/elder', '/tasks']) {
  test(`E01: home ${route} from a hidden generation cannot update the shown page`, async () => {
    const e = setup('index/index');
    if (route === '/tasks') e.view.loadTasks(7);
    const old = requests(e, route).at(-1); e.view.onHide(); e.view.onShow();
    if (route === '/tasks') e.view.loadTasks(7);
    const current = requests(e, route).at(-1);
    success(current, route === '/elder' ? [elder('new')] : [task(22)]); await flush();
    success(old, route === '/elder' ? [elder('old')] : [task(11)]); await flush();
    if (route === '/elder') assert.equal(e.view.data.elder.name, 'new');
    else assert.deepEqual(e.view.data.tasks, [task(22)]);
  });
}

for (const route of ['/elder', '/tasks']) {
  test(`E01: home ${route} ignores an old session with identical credential and elder`, async () => {
    const e = setup('index/index'); if (route === '/tasks') e.view.loadTasks(7);
    const old = requests(e, route).at(-1); await login(e);
    e.view.setData({ elderId: 7, elder: elder('new'), tasks: [task(22)] });
    const before = structuredClone(e.view.data), writes = e.view.writes.length;
    success(old, route === '/elder' ? [elder('old')] : [task(11)]); await flush();
    assert.deepEqual(e.view.data, before); assert.equal(e.view.writes.length, writes);
  });
}

test('E01: elder switch drops an old empty home response instead of clearing the new elder', async () => {
  const e = setup('index/index'), old = requests(e, '/elder')[0];
  e.app.selectAlertElder(8); success(requests(e, '/elder').at(-1), [elder('new', 8)]); await flush();
  success(old, []); await flush(); assert.equal(e.view.data.elderId, 8);
});

for (const outcome of [200, 'network', 403]) {
  test(`E01: unloaded home ${outcome} cannot affect a reopened instance`, async () => {
    const e = setup('index/index'), old = requests(e, '/elder')[0]; e.view.onUnload();
    const writes = e.view.writes.length, fresh = e.pageEntry('index/index'); fresh.onShow();
    success(requests(e, '/elder').at(-1), [elder('new')]); await flush();
    finish(old, outcome, [elder('old')]); await flush();
    assert.equal(e.view.writes.length, writes); assert.equal(fresh.data.elder.name, 'new');
  });
}

test('E02: reset invalidates old alert refresh and permits a fresh same-token same-elder refresh', async () => {
  const e = setup('alerts/alerts'), old = requests(e, '/alerts')[0]; e.view.loadAlerts();
  await login(e); const count = requests(e, '/alerts').length; e.view.loadAlerts();
  assert.equal(requests(e, '/alerts').length, count + 1);
  const fresh = requests(e, '/alerts').at(-1); success(fresh, [message(22).data]); await flush();
  const before = structuredClone(e.view.data), total = e.requests.length;
  success(old, [message(11).data]); await flush();
  assert.deepEqual(e.view.data, before); assert.equal(e.requests.length, total);
});

for (const outcome of [200, 'network', 403]) {
  test(`E02: old refresh ${outcome} finally cannot release or restart a newer in-flight refresh`, async () => {
    const e = setup('alerts/alerts'), old = requests(e, '/alerts')[0]; e.view.loadAlerts();
    e.app.selectAlertElder(8); e.app.selectAlertElder(7);
    const current = e.view._alertRequest, total = e.requests.length;
    finish(old, outcome, [message(11).data]); await flush();
    assert.equal(e.view._alertRequest, current); assert.equal(e.requests.length, total);
    assert.deepEqual(e.view.data.alerts, []);
    success(requests(e, '/alerts').at(-1), [message(22).data]); await flush();
    assert.deepEqual(e.view.data.alerts, [message(22).data]);
  });
}

test('E02: coalesced refresh retains sorting, elder filtering, dedup, failure and empty semantics', async () => {
  const e = setup('alerts/alerts');
  success(requests(e, '/alerts')[0], [message(11).data, message(12, 8).data, message(13).data, message(13).data]); await flush();
  assert.deepEqual(e.view.data.alerts.map((row) => row.id), [11, 13]);
  e.view.loadAlerts(); const first = requests(e, '/alerts').at(-1); const total = requests(e, '/alerts').length;
  for (let i = 0; i < 10; i++) e.view.loadAlerts();
  assert.equal(requests(e, '/alerts').length, total);
  finish(first, 'network'); await flush(); assert.deepEqual(e.view.data.alerts.map((row) => row.id), [11, 13]);
  assert.equal(requests(e, '/alerts').length, total + 1);
  success(requests(e, '/alerts').at(-1), []); await flush(); assert.deepEqual(e.view.data.alerts, []);
});

for (const transition of ['logout', 'login', 'hide-show', 'elder', 'configuration']) {
  for (const outcome of [200, 'network', 401, 403]) {
    test(`E02: cancel completion ${outcome} after ${transition} cannot prompt or refresh`, async () => {
      const e = setup('alerts/alerts'); e.view.cancelAlert({ currentTarget: { dataset: { id: 11 } } });
      const old = requests(e, '/alerts/11/cancel', 'POST')[0];
      if (transition === 'logout') e.app.clearAlertSession();
      if (transition === 'login') await login(e);
      if (transition === 'hide-show') { e.view.onHide(); e.view.onShow(); }
      if (transition === 'elder') e.app.selectAlertElder(8);
      if (transition === 'configuration') e.app.configureEnvironment('test', { environment: 'test', source: 'offline-test',
        baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'] });
      const before = e.requests.length, writes = e.view.writes.length;
      finish(old, outcome); await flush();
      assert.equal(e.toasts.length, 0); assert.equal(e.requests.length, before);
      // Current-session 401 may legitimately cause the application's reset notification.
      if (!(transition === 'hide-show' || transition === 'elder') || outcome !== 401) assert.equal(e.view.writes.length, writes);
    });
  }
}

test('E02: newest cancellation owns prompt and refresh; failed current request never claims success', async () => {
  const e = setup('alerts/alerts');
  e.view.cancelAlert({ currentTarget: { dataset: { id: 11 } } }); const old = requests(e, '/alerts/11/cancel', 'POST')[0];
  e.view.cancelAlert({ currentTarget: { dataset: { id: 22 } } }); success(requests(e, '/alerts/22/cancel', 'POST')[0], {}); await flush();
  success(old, {}); await flush(); assert.deepEqual(e.toasts, ['告警已取消']);
  e.view.cancelAlert({ currentTarget: { dataset: { id: 33 } } }); finish(requests(e, '/alerts/33/cancel', 'POST')[0], 403); await flush();
  assert.deepEqual(e.toasts, ['告警已取消']);
});

for (const entry of ['elder/detail', 'tasks/tasks']) {
  for (const outcome of [200, 'network', 401, 403]) {
    test(`E03: ${entry} old ${outcome} cannot write after same-token login`, async () => {
      const e = setup(entry), old = e.pending[0]; await login(e);
      const before = structuredClone(e.view.data), writes = e.view.writes.length;
      finish(old, outcome, entry === 'elder/detail' ? elder('old') : [task(11)]); await flush();
      assert.deepEqual(e.view.data, before); assert.equal(e.view.writes.length, writes);
    });
  }

  test(`E03: ${entry} old finally cannot start new-scope work or clear its loading state`, async () => {
    const e = setup(entry), old = e.pending[0]; await login(e, 8);
    const before = e.requests.length, state = structuredClone(e.view.data);
    finish(old, 'network'); await flush();
    assert.equal(e.requests.length, before); assert.deepEqual(e.view.data, state);
  });

  test(`E03: ${entry} configuration change invalidates catch and finally`, async () => {
    const e = setup(entry), old = e.pending[0], token = e.app.globalData.token;
    e.app.configureEnvironment('test', { environment: 'test', source: 'offline-test',
      baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'] });
    e.app.globalData.token = token; e.app._boundElders = [7]; e.app.selectAlertElder(7);
    const before = structuredClone(e.view.data), writes = e.view.writes.length;
    finish(old, 200, entry === 'elder/detail' ? elder('old') : [task(11)]); await flush();
    assert.deepEqual(e.view.data, before); assert.equal(e.view.writes.length, writes);
  });

  test(`E03: ${entry} current 401 retains expired feedback and finishes loading`, async () => {
    const e = setup(entry); finish(e.pending[0], 401); await flush();
    assert.ok(e.app.globalData.token === ''); assert.equal(e.view.data.error, '登录已失效，请重新登录');
    assert.equal(e.view.data.loading, false);
  });
}

test('E01/E05: old application binding request finally cannot release a newer request', async () => {
  const e = setup('alerts/alerts'); e.app.refreshAlertBinding(); const old = e.pending.at(-1);
  await login(e); e.app.refreshAlertBinding(); const current = e.app._bindingRequest;
  success(old, [elder('old', 8)]); await flush();
  assert.equal(e.app._bindingRequest, current); assert.equal(e.app.globalData.currentElderId, 7);
  success(e.pending.at(-1), [elder('new')]); await current.promise;
});

test('API: asynchronous HTTP failure with empty body rejects instead of throwing or staying pending', async () => {
  const e = setup('alerts/alerts'), api = require('../utils/api');
  const result = api.get('/tasks').then(() => ({ success: true }), (error) => ({ success: false, status: error.statusCode }));
  assert.doesNotThrow(() => e.pending.at(-1).success({ statusCode: 503, data: null }));
  assert.deepEqual(await result, { success: false, status: 503 });
});
