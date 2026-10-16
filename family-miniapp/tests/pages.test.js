/**
 * P7 家属端小程序页面逻辑 mock 测试（Node 内置 test 运行器）
 *
 * 覆盖验收点：
 *  - P7-2 登录页：空输入提示、成功存 token + 跳首页、失败提示
 *  - P7-3 首页：未绑定/无 token 跳登录（AQ01 前端守卫）、
 *              拉取绑定老人 + 提醒任务 + 最近告警（仅绑定数据）
 *  - P7-4 告警页：GET /alerts 渲染（修正 /alerts/1 错误端点）、取消后刷新
 *  - P7-5 设置页：绑定老人、隐私开关 POST /privacy/auth、
 *                新增提醒任务 POST /tasks（FR-FL-05）、退出登录
 *
 * 运行：cd family-miniapp && node --test tests/
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

/** 按完整 URL 路由返回响应或 fail 的 wx.request mock */
function makeWxMock(routes) {
  const calls = [];
  const wx = {
    request(opts) {
      calls.push(opts);
      const r = routes[opts.url] || routes.default;
      if (!r) return;
      if (r.fail) opts.fail(r.fail);
      else opts.success({ statusCode: r.statusCode, data: r.body });
    },
    setStorageSync() {},
    getStorageSync() {},
    removeStorageSync() {},
    switchTab(o) { wx._switchTab = o; },
    redirectTo(o) { wx._redirect = o; },
    navigateTo() {},
    showToast(o) { wx._toast = o; },
  };
  return { wx, calls };
}

/** 加载页面 js，mock Page/getApp/wx，返回带 data+setData 的实例 */
function loadPage(relPath, { wxMock, appMock }) {
  global.getApp = () => appMock;
  global.wx = wxMock;
  let cfg;
  global.Page = (c) => { cfg = c; };
  const apiAbs = path.resolve(__dirname, '../utils/api.js');
  const pageAbs = path.resolve(__dirname, relPath);
  delete require.cache[apiAbs];
  delete require.cache[pageAbs];
  require(pageAbs);
  const inst = Object.assign({}, cfg);
  inst.data = JSON.parse(JSON.stringify(cfg.data));
  inst.setData = (patch) => Object.assign(inst.data, patch);
  return inst;
}

function makeApp(token) {
  return { globalData: { baseUrl: 'https://srv/api/v1', token: token || '' } };
}

function flush() { return new Promise((r) => setImmediate(r)); }

// ---------- P7-2 登录页 ----------

test('P7-2 login：空输入提示', () => {
  const { wx } = makeWxMock({});
  const page = loadPage('../pages/login/login.js', { wxMock: wx, appMock: makeApp() });
  page.login();
  assert.strictEqual(wx._toast.title, '请输入手机号和密码');
});

test('P7-2 login：成功存 token 并跳首页', () => {
  const { wx, calls } = makeWxMock({
    'https://srv/api/v1/auth/login': { statusCode: 200, body: { access_token: 'tok-x' } },
  });
  const app = makeApp();
  const page = loadPage('../pages/login/login.js', { wxMock: wx, appMock: app });
  page.data.phone = '13800000000';
  page.data.password = 'pw';
  page.login();
  assert.strictEqual(calls.length, 1);
  assert.ok(calls[0].url.endsWith('/auth/login'));
  assert.strictEqual(app.globalData.token, 'tok-x');
  assert.strictEqual(wx._switchTab.url, '/pages/index/index');
});

test('P7-2 login：失败提示', () => {
  const { wx } = makeWxMock({
    'https://srv/api/v1/auth/login': { statusCode: 400, body: { message: '用户名或密码错误' } },
  });
  const page = loadPage('../pages/login/login.js', { wxMock: wx, appMock: makeApp() });
  page.data.phone = 'x';
  page.data.password = 'y';
  page.login();
  assert.strictEqual(wx._toast.title, '登录失败');
});

// ---------- P7-3 首页 ----------

test('P7-3 index：无 token（未绑定）跳登录', () => {
  const { wx } = makeWxMock({});
  const page = loadPage('../pages/index/index.js', { wxMock: wx, appMock: makeApp() });
  page.onLoad();
  assert.strictEqual(wx._redirect.url, '/pages/login/login');
});

test('P7-3 index：拉取绑定老人 + 提醒任务 + 最近告警（仅绑定数据）', async () => {
  const routes = {
    'https://srv/api/v1/elder': {
      statusCode: 200,
      body: { code: 200, data: [{ id: 7, name: '张奶奶', age: 82, privacy_enabled: true }] },
    },
    'https://srv/api/v1/tasks': {
      statusCode: 200,
      body: { code: 200, data: [
        { id: 1, elder_id: 7, task_type: 'medication', title: '服药', is_active: true },
        { id: 2, elder_id: 7, task_type: 'rest', title: '午休', is_active: false },
      ] },
    },
    'https://srv/api/v1/alerts': {
      statusCode: 200,
      body: { code: 200, data: [{ id: 1, alert_type: 'fall', priority: 'P0', status: 'pending', created_at: '2026-10-16T08:00:00' }] },
    },
  };
  const { wx, calls } = makeWxMock(routes);
  const page = loadPage('../pages/index/index.js', { wxMock: wx, appMock: makeApp('tok') });
  page.onLoad();
  await flush();
  assert.strictEqual(page.data.elderId, 7);
  assert.strictEqual(page.data.elder.name, '张奶奶');
  assert.strictEqual(page.data.tasks.length, 2);
  assert.strictEqual(page.data.alerts.length, 1);
  assert.ok(calls.every((c) => c.header && c.header.Authorization === 'Bearer tok'));
  assert.ok(calls.some((c) => c.url.endsWith('/elder') && c.method === 'GET'));
  assert.ok(calls.some((c) => c.url.endsWith('/alerts') && c.method === 'GET'));
});

// ---------- P7-4 告警记录页 ----------

test('P7-4 alerts：GET /alerts 渲染列表', async () => {
  const { wx, calls } = makeWxMock({
    'https://srv/api/v1/alerts': {
      statusCode: 200,
      body: { code: 200, data: [{ id: 1, alert_type: 'fall', priority: 'P0', status: 'pending' }] },
    },
  });
  const page = loadPage('../pages/alerts/alerts.js', { wxMock: wx, appMock: makeApp('tok') });
  page.onShow();
  await flush();
  assert.strictEqual(page.data.alerts.length, 1);
  assert.ok(calls[0].url.endsWith('/alerts'));
  assert.ok(calls[0].url.endsWith('/alerts') && !calls[0].url.endsWith('/alerts/1'));
});

test('P7-4 alerts：取消告警 POST /alerts/{id}/cancel 后刷新', async () => {
  const { wx, calls } = makeWxMock({
    'https://srv/api/v1/alerts': {
      statusCode: 200,
      body: { code: 200, data: [{ id: 1, alert_type: 'fall', priority: 'P0', status: 'pending' }] },
    },
    'https://srv/api/v1/alerts/1/cancel': { statusCode: 200, body: { code: 200, data: { message: '告警已取消' } } },
  });
  const page = loadPage('../pages/alerts/alerts.js', { wxMock: wx, appMock: makeApp('tok') });
  page.onShow();
  await flush();
  page.cancelAlert({ currentTarget: { dataset: { id: 1 } } });
  assert.ok(calls.some((c) => c.url.endsWith('/alerts/1/cancel') && c.method === 'POST'));
  await flush();
  assert.strictEqual(wx._toast.title, '告警已取消');
});

// ---------- P7-5 设置页 ----------

test('P7-5 settings：绑定老人 + 隐私开关 + 新增任务 + 退出', async () => {
  const routes = {
    'https://srv/api/v1/elder': {
      statusCode: 200,
      body: { code: 200, data: [{ id: 3, name: '李爷爷', privacy_enabled: false }] },
    },
    'https://srv/api/v1/privacy/auth': { statusCode: 200, body: { code: 200, data: { message: '隐私授权已更新' } } },
    'https://srv/api/v1/tasks': { statusCode: 200, body: { code: 200, data: { message: '任务已添加' } } },
  };
  const { wx, calls } = makeWxMock(routes);
  const app = makeApp('tok');
  const page = loadPage('../pages/settings/settings.js', { wxMock: wx, appMock: app });
  page.onShow();
  await flush();
  assert.strictEqual(page.data.elderId, 3);

  // 隐私开关 → POST /privacy/auth {elder_id, enabled}
  page.togglePrivacy({ detail: { value: true } });
  const priv = calls.find((c) => c.url.endsWith('/privacy/auth'));
  assert.deepStrictEqual(priv.data, { elder_id: 3, enabled: true });
  await flush();
  assert.strictEqual(page.data.privacyEnabled, true);

  // 新增提醒任务 → POST /tasks（携带 elder_id）
  page.setData({ taskForm: { task_type: 'medication', title: '服药', content: '早一粒', schedule_time: '08:00' } });
  page.addTask();
  const add = calls.find((c) => c.url.endsWith('/tasks') && c.method === 'POST');
  assert.strictEqual(add.data.elder_id, 3);
  assert.strictEqual(add.data.title, '服药');

  // 退出登录
  page.logout();
  assert.strictEqual(wx._redirect.url, '/pages/login/login');
  assert.strictEqual(app.globalData.token, '');
});
