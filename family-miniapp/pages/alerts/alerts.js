const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    alerts: [],
  },

  onShow() {
    this.loadAlerts();
  },

  /** 告警记录列表（FR-FL-03，仅本人老人数据，AQ01） */
  loadAlerts() {
    const token = app.globalData.token;
    if (!token) return;
    api.get('/alerts').then((res) => {
      this.setData({ alerts: res.data || [] });
    }).catch(() => {});
  },

  /** 误告警手动取消：状态切 cancelled 并刷新列表 */
  cancelAlert(e) {
    const alertId = e.currentTarget.dataset.id;
    api.post(`/alerts/${alertId}/cancel`).then(() => {
      wx.showToast({ title: '告警已取消' });
      this.loadAlerts();
    }).catch(() => {});
  },
});
