/**
 * 小程序 utils/api.js 的 mock API 测试（P0 基线）
 * 使用 Node 内置 test 运行器（node:test），无需安装 jest 等依赖。
 * 运行：cd family-miniapp && node --test tests/
 *
 * 通过注入全局 wx 与 getApp 来模拟微信运行时，验证 api.js 的
 * 鉴权头携带、2xx 成功、401 跳转登录、非 2xx 错误处理四条路径。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');

/** 构造一个 mock 运行时后加载 api.js，返回其导出对象 */
function loadApi({ wxMock, appMock }) {
  global.getApp = () => appMock;
  global.wx = wxMock;
  delete require.cache[require.resolve('../utils/api.js')];
  return require('../utils/api.js');
}

function makeAppMock(baseUrl, token) {
  return { globalData: { baseUrl, token } };
}

/** 生成一个可编程的 wx.request mock */
function makeWxMock({ statusCode, body }) {
  const calls = [];
  const wx = {
    request(opts) {
      calls.push(opts);
      if (opts.fail) {
        opts.success({ statusCode, data: body });
      }
    },
    redirectTo(opts) {
      wx._redirect = opts;
    },
    _redirect: null,
  };
  return { wx, calls };
}

test('请求携带鉴权 Bearer Token', () => {
  const { wx, calls } = makeWxMock({ statusCode: 200, body: { data: 'ok' } });
  const api = loadApi({ wxMock: wx, appMock: makeAppMock('https://srv/api/v1', 'tok-123') });

  return api.get('/elder/1/logs').then(() => {
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].url, 'https://srv/api/v1/elder/1/logs');
    assert.strictEqual(calls[0].method, 'GET');
    assert.strictEqual(calls[0].header.Authorization, 'Bearer tok-123');
  });
});

test('2xx 响应返回 res.data', () => {
  const { wx } = makeWxMock({ statusCode: 200, body: { code: 200, data: { list: [] } } });
  const api = loadApi({ wxMock: wx, appMock: makeAppMock('https://srv/api/v1', 't') });

  return api.post('/tasks', { title: '服药' }).then((data) => {
    assert.deepStrictEqual(data, { code: 200, data: { list: [] } });
  });
});

test('401 触发跳转登录并 reject', async () => {
  const { wx } = makeWxMock({ statusCode: 401, body: {} });
  const api = loadApi({ wxMock: wx, appMock: makeAppMock('https://srv/api/v1', 't') });

  await assert.rejects(api.get('/alerts'), /未授权/);
  assert.strictEqual(wx._redirect.url, '/pages/login/login');
});

test('非 2xx 返回错误消息并 reject', async () => {
  const { wx } = makeWxMock({ statusCode: 403, body: { message: '越权访问' } });
  const api = loadApi({ wxMock: wx, appMock: makeAppMock('https://srv/api/v1', 't') });

  await assert.rejects(api.del('/tasks/9'), /越权访问/);
});
