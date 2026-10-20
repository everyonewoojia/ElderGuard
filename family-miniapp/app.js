const { AlertSocket } = require('./utils/alert-socket');

App({
  // Populate both only after review; never infer environment or fetch remote configuration.
  deploymentEnvironment: '',
  deploymentConfig: null,
  globalData: {
    baseUrl: '',
    // Exact approved hostnames only. Empty until deployment configuration is approved.
    apiTrustedHosts: [],
    apiEnvironment: '',
    apiConfigSource: '',
    token: '',
    userInfo: null,
    currentElderId: null,
    // Enable only after the deployment's target WeChat SDK/header check.
    alertSocketHeaderVerified: false,
  },
  
  onLaunch() {
    const stored = wx.getStorageSync('token');
    if (this.alertSocket) this.clearAlertSession();
    this.globalData.token = '';
    this.globalData.currentElderId = null;
    this._alertWatches = new Set();
    this._bindingVersion = (this._bindingVersion || 0) + 1;
    this._loginVersion = (this._loginVersion || 0) + 1;
    this._boundElders = [];
    const api = require('./utils/api');
    this.alertSocket = new AlertSocket({
      wx,
      getSession: () => {
        let configuration = null;
        try { configuration = api.getConfig(this); } catch (_) { /* Missing config is closed. */ }
        return {
          token: this.globalData.token, elderId: this.globalData.currentElderId,
          baseUrl: this.globalData.baseUrl, verified: this.globalData.alertSocketHeaderVerified,
          apiTrustedHosts: this.globalData.apiTrustedHosts, configuration,
          bound: this._boundElders.includes(this.globalData.currentElderId),
        };
      },
      onAuthExpired: () => this.clearAlertSession({ expired: true }),
      onTransportFailure: () => this.checkAlertSession(),
    });
    try {
      const active = this.configureEnvironment(this.deploymentEnvironment, this.deploymentConfig);
      if (stored && typeof stored.value === 'string' && /^[A-Za-z0-9_.~-]+$/.test(stored.value)
          && stored.scope === active.scope) {
        this.globalData.token = stored.value;
        wx.setStorageSync('token', stored);
      } else wx.removeStorageSync('token');
    } catch (_) { wx.removeStorageSync('token'); }
  },

  configureEnvironment(expected, config) {
    return require('./utils/api').configureEnvironment(this, expected, config);
  },

  onShow() {
    this.alertSocket.setForeground(true);
    this.refreshAlertBinding();
  },

  onHide() { this.alertSocket.setForeground(false); },

  /** A failed upgrade may expose only onError; REST distinguishes auth rejection. */
  checkAlertSession() {
    const token = this.globalData.token;
    if (!token || this._authProbe) return;
    const api = require('./utils/api');
    // Reuse REST session/config versions: equal token text is not equal login identity.
    const probe = { token, session: this._bindingVersion, config: this._configEpoch };
    this._authProbe = probe;
    probe.promise = api.get('/elder').catch((error) => {
      if (this._authProbe === probe && token === this.globalData.token
          && probe.session === this._bindingVersion && probe.config === this._configEpoch
          && [401, 403].includes(error?.statusCode)) {
        this.clearAlertSession({ expired: true });
      }
    }).finally(() => { if (this._authProbe === probe) this._authProbe = null; });
  },

  /** Resolve binding using REST even when sockets are unsupported or unverified. */
  refreshAlertBinding() {
    const token = this.globalData.token;
    if (!token) return Promise.resolve();
    if (this._bindingRequest && this._bindingRequest.token === token) return this._bindingRequest.promise;
    const version = this._bindingVersion;
    const request = { token };
    const api = require('./utils/api');
    request.promise = api.get('/elder').then((elders) => {
      if (token !== this.globalData.token || version !== this._bindingVersion) return;
      this._boundElders = elders.map((elder) => elder.id);
      const selected = this._boundElders.includes(this.globalData.currentElderId)
        ? this.globalData.currentElderId : this._boundElders[0] || null;
      this.selectAlertElder(selected);
    }).catch(() => {}).finally(() => {
      if (this._bindingRequest === request) this._bindingRequest = null;
    });
    this._bindingRequest = request;
    return request.promise;
  },

  /** Select only a binding obtained from the authenticated REST route. */
  selectAlertElder(id) {
    if (id !== null && !this._boundElders.includes(id)) return;
    const changed = id !== this.globalData.currentElderId;
    this.globalData.currentElderId = id;
    this.alertSocket.sync();
    for (const watch of this._alertWatches) this._attachAlertWatch(watch, changed ? 'context' : 'sync');
  },

  _attachAlertWatch(watch, type = 'sync') {
    if (watch.off) watch.off();
    watch.off = null;
    if (watch.token !== this.globalData.token) return;
    watch.off = this.alertSocket.subscribe(watch.listener);
    try { watch.listener({ type }); } catch (_) { /* One page cannot block others. */ }
  },

  watchAlerts(listener) {
    const watch = { listener, token: this.globalData.token, off: null };
    this._alertWatches.add(watch);
    if (this.globalData.currentElderId !== null) this._attachAlertWatch(watch);
    else this.refreshAlertBinding();
    return () => { if (watch.off) watch.off(); this._alertWatches.delete(watch); };
  },

  /** Invalidate native callbacks, REST scopes and page watches before re-login. */
  clearAlertSession({ expired = false } = {}) {
    ++this._loginVersion;
    this.globalData.token = '';
    this.globalData.currentElderId = null;
    ++this._bindingVersion;
    this._bindingRequest = null;
    this._authProbe = null;
    this._boundElders = [];
    this.alertSocket.stop(expired ? 'auth_failed' : 'closed');
    for (const watch of this._alertWatches) {
      if (watch.off) watch.off();
      try { watch.listener({ type: 'reset' }); } catch (_) { /* Continue cleanup. */ }
    }
    this._alertWatches.clear();
    wx.removeStorageSync('token');
    if (expired) wx.redirectTo({ url: '/pages/login/login' });
  },
  
  login(callback) {
    const api = require('./utils/api');
    let attempt;
    try { attempt = api.beginLoginAttempt(); }
    catch (_) { callback && callback(false); return; }
    const failed = () => {
      if (!api.isLoginCurrent(attempt)) return;
      api.cancelLogin(attempt);
      callback && callback(false);
    };
    wx.login({
      success: (res) => {
        if (!api.isLoginCurrent(attempt)) return;
        if (res.code) {
          wx.request({
            url: `${attempt.baseUrl}/auth/login`,
            method: 'POST',
            data: { code: res.code },
            success: (response) => {
              if (!api.isLoginCurrent(attempt)) return;
              const data = response.data;
              if (!data || data.code !== 200 || !data.data
                  || !api.acceptLogin(attempt, data.data.token, () => callback && callback(true))) failed();
            },
            fail: failed,
          });
        } else failed();
      },
      fail: failed,
    });
  },
});
