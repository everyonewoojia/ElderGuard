const app = getApp();
const api = require('../../utils/api.js');
const alertView = require('../../utils/alert-view');

Page({
  data: {
    alerts: [],
  },

  onShow() {
    alertView.show(this, () => this.loadAlerts());
    this.loadAlerts();
  },

  onHide() { alertView.hide(this); },
  onUnload() { alertView.hide(this); },

  /** 告警记录列表（FR-FL-03，仅本人老人数据，AQ01） */
  loadAlerts() {
    const token = app.globalData.token;
    if (!token) return;
    alertView.refresh(this);
  },

  /** 误告警手动取消：状态切 cancelled 并刷新列表 */
  cancelAlert(e) {
    const alertId = e.currentTarget.dataset.id;
    const request = { token: app.globalData.token, session: app._bindingVersion,
      config: app._configEpoch, view: this._alertVersion, elder: app.globalData.currentElderId };
    this._cancelRequest = request;
    const valid = () => this._cancelRequest === request && this._alertVisible !== false
      && this._alertVersion === request.view && app.globalData.token === request.token
      && app._bindingVersion === request.session && app._configEpoch === request.config
      && app.globalData.currentElderId === request.elder;
    api.post(`/alerts/${alertId}/cancel`).then(() => {
      if (!valid()) return;
      wx.showToast({ title: '告警已取消' });
      if (valid()) this.loadAlerts();
    }).catch(() => {});
  },
});
