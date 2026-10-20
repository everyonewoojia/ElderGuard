'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { runtime, flush } = require('./helpers/runtime');
const entries = ['elder/detail', 'tasks/tasks'];
const elder = { id: 7, name: '合成老人', age: null, phone: null, address: null, privacy_enabled: false };
const tasks = [{ id: 1, elder_id: 7, title: '合成提醒', task_type: 'rest', is_active: true }, { id: 2, elder_id: 8, title: '另一老人提醒', task_type: 'rest', is_active: false }];

function setup(entry, custom = {}, selected) {
  assert.ok(fs.existsSync(path.resolve(__dirname, `../pages/${entry}.js`)), '声明路由缺少实际页面入口');
  const routes = {
    '/elder': { status: 200, body: [elder] },
    '/elder/7': { status: 200, body: elder },
    '/elder/8': { status: 200, body: { ...elder, id: 8, name: '另一合成老人' } },
    '/tasks': { status: 200, body: tasks },
    ...custom,
  };
  // Reuse the existing runtime and production api.js; only wx transport is synthetic.
  const result = runtime(entry, Object.fromEntries(Object.entries(routes)));
  getApp().globalData.currentElderId = selected;
  return { ...result, routes, app: getApp() };
}

for (const entry of entries) {
  test(`${entry}: no-parameter route resolves binding, loads real API shape, repeats replace data`, async () => {
    const e = setup(entry);
    e.page.onLoad({}); await flush();
    assert.equal(e.page.data.elderId, 7);
    assert.equal(e.page.data.loading, false);
    assert.equal(e.page.data.error, '');
    assert.equal(e.page.data.empty, false);
    if (entry.startsWith('elder')) assert.deepEqual(e.page.data.elder, elder);
    else assert.deepEqual(e.page.data.tasks, [tasks[0]]);
    e.page.onShow(); await flush(); e.page.refresh(); await flush();
    if (entry.startsWith('elder')) assert.deepEqual(e.page.data.elder, elder);
    else assert.deepEqual(e.page.data.tasks, [tasks[0]]);
    assert.ok(e.calls.every((call) => call.method === 'GET' && call.header.Authorization === 'Bearer synthetic'));
  });

  test(`${entry}: current elder takes precedence over the first binding`, async () => {
    const e = setup(entry, {}, 8);
    e.page.onLoad(); await flush();
    assert.equal(e.page.data.elderId, 8);
    if (entry.startsWith('elder')) assert.equal(e.page.data.elder.id, 8);
    else assert.deepEqual(e.page.data.tasks, [tasks[1]]);
  });

  test(`${entry}: no bindings is empty, not an error`, async () => {
    const e = setup(entry, { '/elder': { status: 200, body: [] } });
    e.page.onLoad(); await flush();
    assert.equal(e.page.data.elderId, null);
    assert.equal(e.page.data.empty, true);
    assert.equal(e.page.data.error, '');
    assert.equal(e.page.data.loading, false);
    assert.equal(e.calls.length, 1);
  });

  test(`${entry}: loading and network failure are separate from empty success; retry works`, async () => {
    const e = setup(entry, {}, 7);
    let pending;
    e.wx.request = (options) => { pending = options; };
    e.page.onLoad();
    assert.equal(e.page.data.loading, true);
    assert.equal(e.page.data.empty, false);
    pending.fail({ errMsg: 'synthetic offline' }); await flush();
    assert.ok(e.page.data.error);
    assert.equal(e.page.data.empty, false);
    assert.equal(e.page.data.loading, false);
    e.page.refresh(); pending.success({ statusCode: 200, data: entry.startsWith('elder') ? elder : [tasks[0]] }); await flush();
    assert.equal(e.page.data.error, '');
    assert.equal(e.page.data.empty, false);
  });

  for (const status of [401, 403]) {
    test(`${entry}: HTTP ${status} retains auth/permission handling`, async () => {
      const e = setup(entry, {}, 7);
      e.wx.request = (options) => options.success({ statusCode: status, data: { code: status, message: 'synthetic access denied', data: null } });
      e.page.onLoad(); await flush();
      assert.ok(e.page.data.error);
      assert.equal(e.page.data.empty, false);
      assert.equal(e.wx.redirect?.url, status === 401 ? '/pages/login/login' : undefined);
    });
  }

  test(`${entry}: unsupported URL parameters and invalid selection do not issue requests`, async () => {
    const e = setup(entry);
    e.page.onLoad({ elderId: 'invalid' }); await flush();
    assert.ok(e.page.data.error);
    assert.equal(e.calls.length, 0);
    e.app.globalData.currentElderId = -1;
    e.page.onLoad({}); await flush();
    assert.ok(e.page.data.error);
    assert.equal(e.calls.length, 0);
  });

  test(`${entry}: missing token redirects without requests`, async () => {
    const e = setup(entry);
    e.app.globalData.token = '';
    e.page.onLoad(); await flush();
    assert.equal(e.calls.length, 0);
    assert.equal(e.wx.redirect.url, '/pages/login/login');
    assert.equal(e.page.data.loading, false);
  });

  test(`${entry}: elder switch, same-context refresh and unload reject late responses`, async () => {
    const e = setup(entry, {}, 7);
    const requests = [];
    e.wx.request = (options) => requests.push(options);
    const response = (id) => entry.startsWith('elder') ? { ...elder, id } : tasks.filter((task) => task.elder_id === id);
    e.page.onLoad();
    e.app.globalData.currentElderId = 8;
    e.page.onShow();
    requests[1].success({ statusCode: 200, data: response(8) }); await flush();
    requests[0].success({ statusCode: 200, data: response(7) }); await flush();
    assert.equal(e.page.data.elderId, 8);
    e.page.refresh(); e.page.refresh();
    requests[3].success({ statusCode: 200, data: response(8) }); await flush();
    const snapshot = structuredClone(e.page.data);
    requests[2].fail({ errMsg: 'old failure' }); await flush();
    assert.deepEqual(e.page.data, snapshot);
    e.page.refresh();
    e.page.onUnload();
    const unloaded = structuredClone(e.page.data);
    requests[4].success({ statusCode: 200, data: response(7) }); await flush();
    assert.deepEqual(e.page.data, unloaded);
  });

  test(`${entry}: late result after account change cannot expose old data`, async () => {
    const e = setup(entry, {}, 7);
    let pending;
    e.wx.request = (options) => { pending = options; };
    e.page.onLoad();
    e.app.globalData.token = 'new-synthetic-session';
    pending.success({ statusCode: 200, data: entry.startsWith('elder') ? elder : tasks }); await flush();
    assert.equal(e.page.data.elder, null);
    assert.deepEqual(e.page.data.tasks, []);
  });

  test(`${entry}: application binding resolved during loading restarts in the latest elder scope`, async () => {
    const e = setup(entry);
    const requests = [];
    e.wx.request = (options) => requests.push(options);
    e.page.onLoad();
    assert.ok(requests[0].url.endsWith('/elder'));
    e.app.globalData.currentElderId = 8;
    requests[0].success({ statusCode: 200, data: [elder] }); await flush();
    assert.equal(requests.length, 2);
    requests[1].success({ statusCode: 200, data: entry.startsWith('elder') ? { ...elder, id: 8 } : tasks }); await flush();
    assert.equal(e.page.data.elderId, 8);
    assert.equal(e.page.data.loading, false);
    assert.equal(e.page.data.error, '');
  });
}

test('tasks/tasks: an empty task array is a normal empty state', async () => {
  const e = setup('tasks/tasks', { '/tasks': { status: 200, body: [] } }, 7);
  e.page.onLoad(); await flush();
  assert.deepEqual(e.page.data.tasks, []);
  assert.equal(e.page.data.empty, true);
  assert.equal(e.page.data.error, '');
});
