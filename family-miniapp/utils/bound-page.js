'use strict';

/** Shared loading scopes for the two parameter-free, read-only bound-elder routes. */
function createBoundPage(load) {
  return {
    data: { elderId: null, elder: null, tasks: [], loading: false, empty: false, error: '' },

    onLoad(options = {}) {
      this._visible = true;
      this._version = (this._version || 0) + 1;
      this._optionsValid = options !== null && typeof options === 'object' && Object.keys(options).length === 0;
      if (!this._optionsValid) {
        this.setData({ elderId: null, elder: null, tasks: [], loading: false, empty: false, error: '页面参数无效' });
        return;
      }
      this.refresh();
    },

    onShow() {
      this._visible = true;
      if (this._optionsValid !== false) this.refresh();
    },

    onHide() { this._visible = false; this._version = (this._version || 0) + 1; },
    onUnload() { this.onHide(); },

    /** Request generations prevent older success/failure from replacing a new scope. */
    refresh() {
      if (!this._visible || this._optionsValid === false) return;
      const app = getApp();
      const token = app.globalData.token;
      const selected = app.globalData.currentElderId;
      const session = app._bindingVersion, config = app._configEpoch;
      const version = this._version = (this._version || 0) + 1;
      const current = () => this._visible && version === this._version;
      const sameSession = () => token === app.globalData.token && session === app._bindingVersion && config === app._configEpoch;
      const valid = () => current() && sameSession() && selected === app.globalData.currentElderId;
      let authExpired = false;
      const expired = () => authExpired && !app.globalData.token && app._bindingVersion === session + 1
        && app._configEpoch === config;
      this.setData({ elderId: null, elder: null, tasks: [], loading: false, empty: false, error: '' });
      if (!token) {
        this.setData({ error: '请先登录' });
        wx.redirectTo({ url: '/pages/login/login' });
        return;
      }
      if (selected != null && (!Number.isSafeInteger(selected) || selected <= 0)) {
        this.setData({ error: '老人标识无效' });
        return;
      }
      this.setData({ loading: true });
      const api = require('./api');
      (async () => {
        let id = selected;
        if (id == null) {
          const elders = await api.get('/elder');
          if (!valid()) return;
          if (!Array.isArray(elders)) throw new Error('Invalid elder list');
          if (elders.length === 0) {
            this.setData({ empty: true });
            return;
          }
          id = elders[0].id;
          if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Invalid elder ID');
        }
        const result = await load(id);
        if (valid()) this.setData({ ...result, elderId: id });
      })().catch((error) => {
        if (!current()) return;
        authExpired = error?.statusCode === 401 && error.sessionExpired === true;
        if (!valid() && !expired()) return;
        const message = error?.statusCode === 401 ? '登录已失效，请重新登录'
          : error?.statusCode === 403 ? '无权访问该老人数据'
          : error?.statusCode === 404 ? '老人不存在' : '加载失败，请重试';
        this.setData({ error: message, empty: false });
      }).finally(() => {
        if (!current() || (!sameSession() && !expired())) return;
        if (sameSession() && selected !== app.globalData.currentElderId) {
          this.refresh();
        } else this.setData({ loading: false });
      });
    },
  };
}

module.exports = { createBoundPage };
