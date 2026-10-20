'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { environment, flush } = require('./helpers/socket-runtime');

const elders = (id = 7, enabled = false) => [{ id, privacy_enabled: enabled }];
const form = (title = 'synthetic task') => ({ task_type: 'medication', title, content: 'synthetic', schedule_time: '08:00' });
const blank = { task_type: 'medication', title: '', content: '', schedule_time: '' };
function reply(request, data, statusCode = 200) { request.success({ statusCode, data }); }
function settle(request, result) {
  if (result === 'network') request.fail({ errMsg: 'synthetic offline' });
  else reply(request, {}, typeof result === 'number' ? result : 200);
}
function setup() {
  const e = environment({ verified: false }); e.state.defer = true;
  e.toasts = []; e.navigation = [];
  e.wx.showToast = (options) => e.toasts.push(options.title);
  e.wx.redirectTo = (options) => e.navigation.push(options.url);
  e.pageSettings = () => {
    const page = e.page('settings'); page.writes = [];
    const set = page.setData;
    page.setData = (patch) => { page.writes.push(structuredClone(patch)); set(patch); };
    return page;
  };
  e.settings = e.pageSettings(); e.settings.onShow();
  return e;
}
async function loaded() {
  const e = setup(); reply(e.pending[0], elders()); await flush(); return e;
}
function start(e, kind) {
  if (kind === 'get') e.settings.loadSettings();
  if (kind === 'privacy') e.settings.togglePrivacy({ detail: { value: true } });
  if (kind === 'task') { e.settings.setData({ taskForm: form() }); e.settings.addTask(); }
  return e.pending.at(-1);
}
async function switchAccount(e) {
  e.app.clearAlertSession(); e.app.globalData.token = 'synthetic.account-b';
  e.settings.onShow(); reply(e.pending.at(-1), elders(8, false)); await flush();
}

test('S01: logout clears settings and late GET cannot restore binding or feedback', async () => {
  const e = setup(), old = e.pending[0]; e.settings.logout();
  const writes = e.settings.writes.length;
  reply(old, elders(7, true)); await flush();
  assert.equal(e.settings.data.elderId, null); assert.equal(e.settings.data.privacyEnabled, false);
  assert.equal(e.settings.writes.length, writes); assert.equal(e.toasts.length, 0);
  assert.ok(e.app.globalData.token === ''); assert.equal(e.navigation.length, 1);
});

for (const order of ['A-first', 'B-first']) {
  test(`S02: account switch keeps B settings in ${order} response order`, async () => {
    const e = setup(), old = e.pending[0];
    e.app.clearAlertSession(); e.app.globalData.token = 'synthetic.account-b'; e.settings.onShow();
    const fresh = e.pending.at(-1);
    if (order === 'A-first') { reply(old, elders(7, true)); await flush(); reply(fresh, elders(8, false)); }
    else { reply(fresh, elders(8, false)); await flush(); reply(old, elders(7, true)); }
    await flush(); assert.equal(e.settings.data.elderId, 8); assert.equal(e.settings.data.privacyEnabled, false);
    assert.equal(e.toasts.length, 0); assert.ok(e.app.globalData.token === 'synthetic.account-b');
  });
}

for (const kind of ['privacy', 'task']) {
  for (const transition of ['logout', 'account']) {
    for (const result of ['success', 'network', 401, 403]) {
      test(`S03/S05/S07: old ${kind} ${result} after ${transition} has no UI effects`, async () => {
        const e = await loaded(), old = start(e, kind);
        if (transition === 'logout') e.settings.logout(); else await switchAccount(e);
        e.settings.setData({ taskForm: form('new draft') });
        const before = structuredClone(e.settings.data), writes = e.settings.writes.length;
        const nav = e.navigation.length;
        settle(old, result); await flush();
        assert.deepEqual(e.settings.data, before); assert.equal(e.settings.writes.length, writes);
        assert.equal(e.toasts.length, 0); assert.equal(e.navigation.length, nav);
        assert.ok(e.app.globalData.token === (transition === 'logout' ? '' : 'synthetic.account-b'));
        assert.equal(e.connections.length, 0);
      });
    }
  }
}

for (const kind of ['get', 'privacy', 'task']) {
  for (const result of ['success', 'network', 403]) {
    test(`S04: ${kind} ${result} after unload cannot update old or reopened page`, async () => {
      const e = await loaded(), oldPage = e.settings, old = start(e, kind);
      oldPage.onUnload?.();
      const oldWrites = oldPage.writes.length;
      const reopened = e.pageSettings(); reopened.onShow(); reply(e.pending.at(-1), elders(8)); await flush();
      const current = structuredClone(reopened.data);
      if (kind === 'get' && result === 'success') reply(old, elders(7, true)); else settle(old, result);
      await flush(); assert.equal(oldPage.writes.length, oldWrites);
      assert.deepEqual(reopened.data, current); assert.equal(e.toasts.length, 0);
    });
  }
}

test('S04: hidden page callbacks are invalidated even after showing same instance again', async () => {
  const e = await loaded(), old = start(e, 'privacy'); e.settings.onHide?.();
  e.settings.onShow(); reply(e.pending.at(-1), elders(7, false)); await flush();
  reply(old, {}); await flush();
  assert.equal(e.settings.data.privacyEnabled, false); assert.equal(e.toasts.length, 0);
});

test('S06: newest GET wins; failed refresh preserves data instead of becoming empty success', async () => {
  const e = setup(), old = e.pending[0]; e.settings.loadSettings();
  reply(e.pending.at(-1), elders(8, true)); await flush(); reply(old, elders(7, false)); await flush();
  assert.equal(e.settings.data.elderId, 8); assert.equal(e.settings.data.privacyEnabled, true);
  e.settings.loadSettings(); e.pending.at(-1).fail({ errMsg: 'synthetic offline' }); await flush();
  assert.equal(e.settings.data.elderId, 8); assert.equal(e.settings.data.privacyEnabled, true);
});

test('S06: newest privacy operation wins; stale GET cannot roll it back', async () => {
  const e = await loaded(); const get = start(e, 'get'); const old = start(e, 'privacy');
  e.settings.togglePrivacy({ detail: { value: false } }); const fresh = e.pending.at(-1);
  reply(fresh, {}); await flush(); reply(old, {}); reply(get, elders(7, true)); await flush();
  assert.equal(e.settings.data.privacyEnabled, false); assert.deepEqual(e.toasts, ['设置已更新']);
});

test('S06: newer REST refresh supersedes pending privacy callback', async () => {
  const e = await loaded(), old = start(e, 'privacy'); e.settings.loadSettings();
  reply(e.pending.at(-1), elders(7, false)); await flush(); reply(old, {}); await flush();
  assert.equal(e.settings.data.privacyEnabled, false); assert.equal(e.toasts.length, 0);
});

test('S05/S06: old task success cannot erase a newer edited draft', async () => {
  const e = await loaded(), old = start(e, 'task');
  e.settings.onTaskInput({ currentTarget: { dataset: { field: 'title' } }, detail: { value: 'new draft' } });
  reply(old, {}); await flush();
  assert.equal(e.settings.data.taskForm.title, 'new draft'); assert.equal(e.toasts.length, 0);
});

test('S05/S06: newest task submission owns success and form cleanup', async () => {
  const e = await loaded(), old = start(e, 'task');
  e.settings.setData({ taskForm: form('new submission') }); e.settings.addTask();
  reply(e.pending.at(-1), {}); await flush();
  e.settings.setData({ taskForm: form('next draft') }); reply(old, {}); await flush();
  assert.equal(e.settings.data.taskForm.title, 'next draft'); assert.deepEqual(e.toasts, ['任务已添加']);
});

for (const kind of ['get', 'privacy', 'task']) {
  test(`S07: selected elder change invalidates pending ${kind}`, async () => {
    const e = await loaded(), old = start(e, kind);
    e.app._boundElders = [7, 8]; e.app.selectAlertElder(8);
    const before = structuredClone(e.settings.data), writes = e.settings.writes.length;
    if (kind === 'get') reply(old, elders(7, true)); else reply(old, {});
    await flush(); assert.deepEqual(e.settings.data, before);
    assert.equal(e.settings.writes.length, writes); assert.equal(e.toasts.length, 0);
  });
}

test('S07: actions cannot submit an old binding after session changes without a page refresh', async () => {
  const e = await loaded(), count = e.requests.length;
  e.app.clearAlertSession(); e.app.globalData.token = 'synthetic.account-b';
  e.settings.togglePrivacy({ detail: { value: true } });
  e.settings.setData({ taskForm: form('new draft') }); e.settings.addTask();
  assert.equal(e.requests.length, count); assert.equal(e.toasts.length, 0);
});

for (const result of ['success', 'network', 401, 403]) {
  test(`S07: actual login with reused opaque token survives old settings ${result}`, async () => {
    const e = await loaded(), old = start(e, 'privacy'), token = e.app.globalData.token;
    const login = e.page('login'); login.setData({ phone: 'synthetic-user', password: 'synthetic-password' });
    login.login(); reply(e.pending.at(-1), { access_token: token });
    reply(e.pending.at(-1), elders(8)); await flush();
    e.settings.onShow(); reply(e.pending.at(-1), elders(8)); await flush();
    const before = structuredClone(e.settings.data), nav = e.navigation.length;
    settle(old, result); await flush();
    assert.ok(e.app.globalData.token === token); assert.equal(e.app.globalData.currentElderId, 8);
    assert.deepEqual(e.settings.data, before); assert.equal(e.navigation.length, nav); assert.equal(e.toasts.length, 0);
  });
}

test('S07: configuration switch rejects old results and allows fresh page work', async () => {
  const e = await loaded(), old = start(e, 'task');
  e.app.configureEnvironment('test', { environment: 'test', source: 'offline-test',
    baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'] });
  e.app.globalData.token = 'synthetic.next'; e.settings.onShow();
  reply(e.pending.at(-1), elders(8)); await flush(); reply(old, {}); await flush();
  assert.equal(e.settings.data.elderId, 8); assert.equal(e.toasts.length, 0);
  const fresh = start(e, 'privacy'); reply(fresh, {}); await flush();
  assert.deepEqual(e.toasts, ['设置已更新']); assert.equal(e.connections.length, 0);
});

test('S08: normal load, authorization/revocation and task body, toast and cleanup remain compatible', async () => {
  const e = await loaded(); assert.equal(e.settings.data.elderId, 7);
  for (const enabled of [true, false]) {
    e.settings.togglePrivacy({ detail: { value: enabled } }); const request = e.pending.at(-1);
    assert.deepEqual(request.data, { elder_id: 7, enabled }); reply(request, {}); await flush();
    assert.equal(e.settings.data.privacyEnabled, enabled);
  }
  const request = start(e, 'task');
  assert.equal(request.method, 'POST'); assert.ok(request.url.endsWith('/tasks'));
  assert.deepEqual(request.data, { elder_id: 7, ...form() }); reply(request, { id: 1 }); await flush();
  assert.deepEqual(e.settings.data.taskForm, blank);
  assert.deepEqual(e.toasts, ['设置已更新', '设置已更新', '任务已添加']);
});

for (const result of ['network', 401, 403]) {
  test(`S08: current privacy ${result} preserves failure and authentication semantics`, async () => {
    const e = await loaded(), old = start(e, 'privacy'); settle(old, result); await flush();
    assert.equal(e.settings.data.privacyEnabled, false); assert.equal(e.toasts.length, 0);
    assert.ok(result === 401 ? e.app.globalData.token === '' : Boolean(e.app.globalData.token));
    assert.equal(e.navigation.length, result === 401 ? 1 : 0);
  });
}

test('S08: task failure preserves the draft and never claims success', async () => {
  const e = await loaded(), old = start(e, 'task'); old.fail({ errMsg: 'synthetic offline' }); await flush();
  assert.deepEqual(e.settings.data.taskForm, form()); assert.equal(e.toasts.length, 0);
});

test('S08: empty list is legitimate; visible task fields are bound to the guarded form state', async () => {
  const e = setup(); reply(e.pending[0], []); await flush();
  assert.equal(e.settings.data.elderId, null); assert.equal(e.settings.data.privacyEnabled, false);
  const template = fs.readFileSync(path.join(__dirname, '../pages/settings/settings.wxml'), 'utf8');
  for (const field of ['title', 'content', 'schedule_time']) {
    const input = template.match(new RegExp(`<input[^>]*data-field="${field}"[^>]*>`));
    assert.ok(input, 'task input is missing');
    assert.ok(input[0].includes(`value="{{taskForm.${field}}}"`), 'task input must display guarded form data');
  }
});

test('S08: missing binding or task title retains the original validation feedback', async () => {
  const e = setup(); reply(e.pending[0], []); await flush();
  e.settings.setData({ taskForm: form() }); e.settings.addTask();
  assert.deepEqual(e.toasts, ['请先绑定老人并填写任务标题']);
  e.settings.loadSettings(); reply(e.pending.at(-1), elders()); await flush();
  e.settings.setData({ taskForm: blank }); e.settings.addTask();
  assert.deepEqual(e.toasts, ['请先绑定老人并填写任务标题', '请先绑定老人并填写任务标题']);
  assert.equal(e.requests.filter((request) => request.method === 'POST').length, 0);
});

test('S08: application binding resolved during loading restarts GET in the latest scope', async () => {
  const e = setup(), old = e.pending[0];
  e.app._boundElders = [7, 8]; e.app.selectAlertElder(8);
  reply(old, elders(7, true)); await flush();
  assert.equal(e.settings.data.elderId, null);
  assert.equal(e.pending.length, 2, 'current binding needs a fresh settings request');
  reply(e.pending[1], elders(8, false)); await flush();
  assert.equal(e.settings.data.elderId, 8); assert.equal(e.settings.data.privacyEnabled, false);
  assert.equal(e.toasts.length, 0);
});

test('S08: privacy action without a binding cannot cancel a valid pending refresh', async () => {
  const e = setup(); reply(e.pending[0], []); await flush();
  e.settings.loadSettings(); const current = e.pending.at(-1);
  e.settings.togglePrivacy({ detail: { value: true } });
  reply(current, elders(7)); await flush();
  assert.equal(e.settings.data.elderId, 7);
  assert.equal(e.requests.filter((request) => request.method === 'POST').length, 0);
});
