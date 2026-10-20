'use strict';
const path = require('node:path');

/** Deterministic timers; no wall-clock sleeps or network connections. */
class Clock {
  constructor() { this.now = 0; this.next = 0; this.jobs = new Map(); }
  set = (fn, delay) => { const id = ++this.next; this.jobs.set(id, { fn, at: this.now + delay }); return id; };
  clear = (id) => this.jobs.delete(id);
  tick(ms) {
    const end = this.now + ms;
    while (true) {
      const entry = [...this.jobs].filter(([, j]) => j.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!entry) break;
      this.now = entry[1].at;
      this.jobs.delete(entry[0]);
      entry[1].fn();
    }
    this.now = end;
  }
}

/** Keep listeners to exercise callbacks arriving after native close/cancellation. */
class Socket {
  constructor() { this.handlers = {}; this.closes = 0; }
  onOpen(fn) { this.handlers.Open = fn; }
  onMessage(fn) { this.handlers.Message = fn; }
  onClose(fn) { this.handlers.Close = fn; }
  onError(fn) { this.handlers.Error = fn; }
  emit(name, value = {}) { this.handlers[name]?.(value); }
  close(opts) { this.closeOptions = opts; this.closes++; if (this.throwClose) throw new Error('synthetic private transport'); if (this.failClose) opts.fail({ errMsg: 'synthetic private transport' }); }
}

/** Load the actual application and pages behind synthetic wx APIs. */
function environment({ token = 'synthetic.token.value', verified = true, configured = true } = {}) {
  const clock = new Clock();
  const sockets = [], connections = [], requests = [], pending = [];
  const state = { alerts: [], elders: [{ id: 7, privacy_enabled: true }, { id: 8, privacy_enabled: false }], failRest: false, defer: false, throwConnect: false, immediateFail: false, httpStatus: 200 };
  if (process.env.ELDERGUARD_WS_ELDERS) state.elders = JSON.parse(process.env.ELDERGUARD_WS_ELDERS);
  const wx = {
    getStorageSync: () => token,
    setStorageSync() {}, removeStorageSync() {},
    getAppBaseInfo: () => ({ SDKVersion: '3.7.0' }),
    canIUse: () => true,
    redirectTo(opts) { wx.redirect = opts; }, switchTab() {}, showToast() {},
    connectSocket(opts) {
      connections.push(opts);
      if (state.throwConnect) throw new Error('synthetic private transport');
      const socket = new Socket(); sockets.push(socket);
      if (state.immediateFail) opts.fail({ errMsg: 'synthetic private transport' });
      return socket;
    },
    request(opts) {
      requests.push(opts);
      if (state.defer) { pending.push(opts); return; }
      if (state.failRest) { opts.fail({ errMsg: 'synthetic offline' }); return; }
      if (state.httpStatus !== 200) { opts.success({ statusCode: state.httpStatus, data: { message: 'synthetic auth rejected' } }); return; }
      const route = opts.url.split('/api/v1')[1];
      opts.success({ statusCode: 200, data: route === '/elder' ? state.elders : route === '/alerts' ? state.alerts : [] });
    },
  };
  global.wx = wx;
  let app;
  global.App = (config) => { app = config; };
  global.getApp = () => app;
  for (const file of ['../../app.js', '../../utils/api.js']) delete require.cache[path.resolve(__dirname, file)];
  require('../../app.js');
  const originalSet = global.setTimeout, originalClear = global.clearTimeout;
  global.setTimeout = clock.set; global.clearTimeout = clock.clear;
  try { app.onLaunch(); } finally { global.setTimeout = originalSet; global.clearTimeout = originalClear; }
  if (configured) {
    app.configureEnvironment('test', { environment: 'test', source: 'offline-test',
      baseUrl: 'https://contract.invalid/api/v1', apiTrustedHosts: ['contract.invalid'] });
    app.globalData.token = token;
    app.globalData.alertSocketHeaderVerified = verified;
  }
  function page(name) {
    let config;
    global.Page = (value) => { config = value; };
    const file = path.resolve(__dirname, `../../pages/${name}/${name}.js`);
    delete require.cache[file]; require(file);
    const instance = { ...config, data: structuredClone(config.data) };
    instance.setData = (patch) => Object.assign(instance.data, patch);
    return instance;
  }
  return { app, wx, clock, sockets, connections, requests, pending, state, page };
}

function message(id = 21, elder = 7) {
  return { type: 'alert', data: { id, elder_id: elder, alert_type: 'fall', priority: 'P0', content: 'synthetic', status: 'pending', created_at: '2026-10-18T01:00:00' } };
}
function flush() { return new Promise((resolve) => setImmediate(resolve)); }
module.exports = { environment, message, flush };
