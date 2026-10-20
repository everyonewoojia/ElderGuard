'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { runtime, flush } = require('./helpers/runtime');

// The cloud integration supplies actual TestClient JSON, without changing bodies.
// Standalone npm tests use the same bare-array/object shapes, without a server.
const fixture = process.env.ELDERGUARD_REST_CONTRACT
  ? JSON.parse(process.env.ELDERGUARD_REST_CONTRACT)
  : {
    populated: {
      '/elder': { status: 200, body: [{ id: 7, name: '合成老人', privacy_enabled: true }] },
      '/tasks': { status: 200, body: [
        { id: 11, elder_id: 7, task_type: 'medication', title: '合成提醒', is_active: true },
        { id: 12, elder_id: 8, task_type: 'rest', title: '另一老人提醒', is_active: true },
      ] },
      '/alerts': { status: 200, body: Array.from({ length: 4 }, (_, i) => ({
        id: 21 + i, elder_id: 7, alert_type: 'fall', status: 'pending',
      })) },
    },
    empty: Object.fromEntries(['/elder', '/tasks', '/alerts'].map((url) => [url, { status: 200, body: [] }])),
    unauthorized: { status: 401, body: { code: 401, message: '未授权', data: null } },
    forbidden: { status: 403, body: { code: 403, message: '无权访问该老人数据', data: null } },
    objects: {
      '/elder/7': { status: 200, body: { id: 7, name: '合成老人', privacy_enabled: true } },
      '/tasks': { status: 200, body: { id: 13, elder_id: 7, title: '新增提醒', is_active: true } },
      '/privacy/auth': { status: 200, body: { message: '隐私授权已更新', enabled: true } },
      '/alerts/21/cancel': { status: 200, body: { message: '告警已取消' } },
    },
  };

test('R01/R02/R03: bare route lists reach index through the production API', async () => {
  const routes = structuredClone(fixture.populated);
  const { page, calls } = runtime('index', routes);
  page.onLoad();
  await flush();
  const elder = routes['/elder'].body[0];
  assert.deepEqual(page.data.elder, elder);
  assert.equal(page.data.elderId, elder.id);
  const tasks = routes['/tasks'].body.filter((task) => task.elder_id === elder.id);
  assert.ok(tasks.length > 0);
  assert.deepEqual(page.data.tasks, tasks);
  assert.equal(page.data.stats.medication, `${tasks.filter((t) => t.is_active && t.task_type === 'medication').length}项`);
  assert.deepEqual(page.data.alerts, routes['/alerts'].body.slice(0, 3));
  assert.equal(calls.length, 3);
  assert.ok(calls.every((c) => c.header.Authorization === 'Bearer synthetic'));
});

test('R01/R03/R08: settings and alerts refresh replace data without duplication', async () => {
  const routes = structuredClone(fixture.populated);
  const settings = runtime('settings', routes).page;
  settings.onShow();
  await flush();
  assert.equal(settings.data.elderId, routes['/elder'].body[0].id);
  assert.equal(settings.data.privacyEnabled, routes['/elder'].body[0].privacy_enabled);
  settings.onShow();
  await flush();
  assert.equal(settings.data.elderId, routes['/elder'].body[0].id);
  const { page } = runtime('alerts', routes);
  page.onShow();
  await flush();
  assert.deepEqual(page.data.alerts, routes['/alerts'].body);
  page.onShow();
  await flush();
  assert.deepEqual(page.data.alerts, routes['/alerts'].body);
  routes['/alerts'] = { status: 200, body: routes['/alerts'].body.slice(0, 1) };
  page.onShow();
  await flush();
  assert.deepEqual(page.data.alerts, routes['/alerts'].body);
});

test('R02/R03/R08: task and recent-alert loaders independently consume and replace bare lists', async () => {
  const routes = structuredClone(fixture.populated);
  const { page } = runtime('index', routes);
  const elderId = routes['/elder'].body[0].id;
  const expected = routes['/tasks'].body.filter((t) => t.elder_id === elderId);
  page.loadTasks(elderId);
  page.loadAlerts();
  await flush();
  assert.deepEqual(page.data.tasks, expected);
  assert.deepEqual(page.data.alerts, routes['/alerts'].body.slice(0, 3));
  page.loadTasks(elderId);
  page.loadAlerts();
  await flush();
  assert.deepEqual(page.data.tasks, expected);
  routes['/tasks'] = fixture.empty['/tasks'];
  routes['/alerts'] = fixture.empty['/alerts'];
  page.loadTasks(elderId);
  page.loadAlerts();
  await flush();
  assert.deepEqual(page.data.tasks, []);
  assert.deepEqual(page.data.alerts, []);
  assert.equal(page.data.stats.medication, '0项');
});

test('R04/R08: a successful empty elder refresh clears earlier binding and dependent state', async () => {
  for (const name of ['index', 'settings']) {
    const routes = structuredClone(fixture.populated);
    const { page, wx } = runtime(name, routes);
    if (name === 'index') page.onLoad();
    else page.onShow();
    await flush();
    assert.notEqual(page.data.elderId, null);
    routes['/elder'] = fixture.empty['/elder'];
    if (name === 'index') page.loadBindElder();
    else page.onShow();
    await flush();
    assert.equal(page.data.elderId, null);
    if (name === 'index') {
      assert.equal(page.data.elder, null);
      assert.deepEqual(page.data.tasks, []);
      assert.deepEqual(page.data.alerts, []);
      assert.equal(page.data.stats.medication, '0项');
    } else assert.equal(page.data.privacyEnabled, false);
    assert.equal(wx.redirect, undefined);
  }
});

test('R04: empty arrays produce normal empty page states', async () => {
  const { page, wx, calls } = runtime('index', fixture.empty);
  page.onLoad();
  page.loadTasks(7);
  page.loadAlerts();
  await flush();
  assert.equal(page.data.elder, null);
  assert.equal(page.data.elderId, null);
  assert.deepEqual(page.data.tasks, []);
  assert.deepEqual(page.data.alerts, []);
  assert.equal(page.data.stats.medication, '0项');
  assert.equal(wx.redirect, undefined);
  assert.equal(calls.length, 3);
  const alerts = runtime('alerts', fixture.empty).page;
  alerts.onShow();
  await flush();
  assert.deepEqual(alerts.data.alerts, []);
  const settings = runtime('settings', fixture.empty).page;
  settings.onShow();
  await flush();
  assert.equal(settings.data.elderId, null);
});

test('R05: network rejection is preserved and does not replace loaded lists with empty success', async () => {
  const error = { errMsg: 'request:fail synthetic offline' };
  const routes = Object.fromEntries(['/elder', '/tasks', '/alerts'].map((url) => [url, { fail: error }]));
  const { api, page, wx } = runtime('index', routes);
  await assert.rejects(api.get('/elder'));
  const previous = { elder: { id: 7 }, elderId: 7, tasks: [{ id: 11 }], alerts: [{ id: 21 }] };
  page.setData(structuredClone(previous));
  page.loadBindElder();
  page.loadTasks(7);
  page.loadAlerts();
  await flush();
  for (const key of Object.keys(previous)) assert.deepEqual(page.data[key], previous[key]);
  assert.equal(wx.redirect, undefined);
});

test('R05 output boundary: reject a controlled error without retaining the native object', async () => {
  const raw = { errMsg: 'request:fail synthetic offline' };
  const { api } = runtime(null, { '/elder': { fail: raw } });
  await assert.rejects(api.get('/elder'), (error) => {
    assert.ok(error instanceof Error);
    assert.notStrictEqual(error, raw);
    assert.equal(error.code, 'NETWORK_FAILURE');
    assert.equal(error.message, '网络请求失败，请重试');
    assert.equal(error.errMsg, undefined);
    assert.equal(error.cause, undefined);
    return true;
  });
  assert.deepEqual(raw, { errMsg: 'request:fail synthetic offline' });
});

for (const [name, response] of [['401', fixture.unauthorized], ['403', fixture.forbidden]]) {
  test(`R06: HTTP ${name} rejects and preserves the existing auth path`, async () => {
    const routes = Object.fromEntries(['/elder', '/tasks', '/alerts'].map((url) => [url, response]));
    const { api, page, wx } = runtime('index', routes);
    await assert.rejects(api.get('/elder'), (error) => {
      assert.equal(error.statusCode, Number(name));
      return true;
    });
    page.setData({ elderId: 99, tasks: [{ id: 1 }], alerts: [{ id: 2 }] });
    page.onLoad();
    page.loadTasks(99);
    page.loadAlerts();
    await flush();
    assert.equal(page.data.elderId, 99);
    assert.deepEqual(page.data.tasks, [{ id: 1 }]);
    assert.deepEqual(page.data.alerts, [{ id: 2 }]);
    assert.equal(wx.redirect?.url, name === '401' ? '/pages/login/login' : undefined);
  });
  test(`R06 output boundary: HTTP ${name} uses fixed text instead of server message`, async () => {
    const { api } = runtime(null, { '/elder': { ...response, body: { message: 'Bearer synthetic.untrusted.server' } } });
    await assert.rejects(api.get('/elder'), (error) => {
      assert.equal(error.statusCode, Number(name));
      assert.equal(error.code, 'HTTP_ERROR');
      assert.equal(error.message, name === '401' ? '未授权' : '无权访问该老人数据');
      assert.ok(!JSON.stringify(error).includes('synthetic.untrusted.server'));
      return true;
    });
  });
}

test('R07: successful objects and existing envelopes pass through unchanged', async () => {
  const envelope = { code: 200, message: 'success', data: { list: [] } };
  const routes = { ...fixture.objects, '/synthetic-envelope': { status: 200, body: envelope } };
  const { api } = runtime(null, routes);
  for (const [url, response] of Object.entries(routes)) {
    const actual = url === '/tasks' || url.endsWith('/auth') || url.endsWith('/cancel')
      ? await api.post(url, {}) : await api.get(url);
    assert.strictEqual(actual, response.body);
    assert.deepEqual(actual, response.body);
  }
});
