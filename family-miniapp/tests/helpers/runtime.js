'use strict';

const path = require('node:path');

/** Load the production API and page with a synthetic WeChat runtime. */
function runtime(pageName, routes) {
  const app = { globalData: { baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'], token: 'synthetic' } };
  const calls = [];
  const wx = {
    request(opts) {
      calls.push(opts);
      const route = routes[opts.url.replace(app.globalData.baseUrl, '')];
      if (!route) throw new Error(`Unexpected contract request: ${opts.url}`);
      if (route.fail) opts.fail(route.fail);
      else opts.success({ statusCode: route.status, data: route.body });
    },
    redirectTo(opts) { wx.redirect = opts; },
    showToast(opts) { wx.toast = opts; },
  };
  global.getApp = () => app;
  global.wx = wx;
  let config;
  global.Page = (value) => { config = value; };
  const apiPath = path.resolve(__dirname, '../../utils/api.js');
  delete require.cache[apiPath];
  const api = require(apiPath);
  api.configureEnvironment(app, 'test', { environment: 'test', source: 'offline-test',
    baseUrl: app.globalData.baseUrl, apiTrustedHosts: app.globalData.apiTrustedHosts });
  let page;
  if (pageName) {
    const entry = pageName.includes('/') ? pageName : `${pageName}/${pageName}`;
    const pagePath = path.resolve(__dirname, `../../pages/${entry}.js`);
    delete require.cache[pagePath];
    require(pagePath);
    page = { ...config, data: structuredClone(config.data) };
    page.setData = (patch) => Object.assign(page.data, patch);
  }
  return { api, page, wx, calls };
}

/** Drain the page's request promise chain. */
function flush() { return new Promise((resolve) => setImmediate(resolve)); }

module.exports = { runtime, flush };
