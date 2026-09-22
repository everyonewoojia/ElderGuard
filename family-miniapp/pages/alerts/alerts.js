const app = getApp();

Page({
  data: {
    alerts: [],
  },

  onShow() {
    this.loadAlerts();
  },

  loadAlerts() {
    const token = app.globalData.token;
    if (!token) return;

    wx.request({
      url: `${app.globalData.baseUrl}/alerts/1`,
      method: 'GET',
      header: { Authorization: `Bearer ${token}` },
      success: (res) => {
        if (res.statusCode === 200) {
          this.setData({ alerts: res.data });
        }
      },
    });
  },

  cancelAlert(e) {
    const alertId = e.currentTarget.dataset.id;
    const token = app.globalData.token;

    wx.request({
      url: `${app.globalData.baseUrl}/alerts/${alertId}/cancel`,
      method: 'POST',
      header: { Authorization: `Bearer ${token}` },
      success: (res) => {
        if (res.statusCode === 200) {
          wx.showToast({ title: '告警已取消' });
          this.loadAlerts();
        }
      },
    });
  },
});
