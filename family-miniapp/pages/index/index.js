const app = getApp();
const api = require('../../utils/api.js');
const alertView = require('../../utils/alert-view');

Page({
  data: {
    elder: null,
    elderId: null,
    tasks: [],
    stats: {
      medication: '0项',
      activity: '-',
      sedentary: '-',
    },
    alerts: [],
  },

  onLoad() {
    alertView.show(this, (event) => this.refreshAlertContext(event));
    this.loadBindElder();
  },

  onShow() {
    alertView.show(this, (event) => this.refreshAlertContext(event));
    this.loadBindElder();
  },
  onHide() { alertView.hide(this); },
  onUnload() { alertView.hide(this); },

  refreshAlertContext(event) {
    if (event.type === 'context') this.loadBindElder();
    this.loadAlerts();
  },

  /** Page/request ownership supplements the shared application session versions. */
  _homeRequest(kind) {
    const request = { kind, token: app.globalData.token, session: app._bindingVersion,
      config: app._configEpoch, view: this._alertVersion, selected: app.globalData.currentElderId };
    this[kind] = request;
    return request;
  },

  _homeCurrent(request) {
    return this[request.kind] === request && this._alertVisible !== false
      && this._alertVersion === request.view && app.globalData.token === request.token
      && app._bindingVersion === request.session && app._configEpoch === request.config
      && app.globalData.currentElderId === request.selected;
  },

  /**
   * 获取当前家属绑定的老人（仅本人数据，AQ01）。
   * 未登录/未绑定 → 跳转登录页（未绑定不可查看）。
   */
  loadBindElder() {
    const token = app.globalData.token;
    if (!token) {
      wx.redirectTo({ url: '/pages/login/login' });
      return;
    }
    const request = this._homeRequest('_homeElderRequest');
    api.get('/elder').then((res) => {
      if (!this._homeCurrent(request)) return;
      this._homeTaskRequest = null;
      const list = res;
      if (list.length === 0) {
        this.setData({
          elder: null, elderId: null, tasks: [], alerts: [],
          stats: { ...this.data.stats, medication: '0项' },
        });
        return;
      }
      const first = list.find((elder) => elder.id === app.globalData.currentElderId) || list[0];
      this.setData({ elder: first, elderId: first.id });
      this.loadTasks(first.id);
      this.loadAlerts();
    }).catch(() => {});
  },

  /** 今日提醒任务（FR-FL-02：老人提醒完成情况） */
  loadTasks(elderId) {
    const request = this._homeRequest('_homeTaskRequest');
    api.get('/tasks').then((res) => {
      if (!this._homeCurrent(request)
          || (app.alertSocket && app.globalData.currentElderId !== elderId)) return;
      const all = res;
      const tasks = all.filter((t) => t.elder_id === elderId);
      const active = tasks.filter((t) => t.is_active);
      this.setData({
        tasks,
        stats: {
          medication: `${active.filter((t) => t.task_type === 'medication').length}项`,
          activity: '-',
          sedentary: '-',
        },
      });
    }).catch(() => {});
  },

  /** 最近告警（FR-FL-03） */
  loadAlerts() {
    alertView.refresh(this, 3);
  },

  goToDetail() {
    wx.navigateTo({ url: '/pages/elder/detail' });
  },
});
