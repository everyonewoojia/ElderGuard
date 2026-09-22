const app = getApp();

Page({
  data: {
    privacyEnabled: false,
    alertEnabled: true,
  },

  onShow() {
    this.loadSettings();
  },

  loadSettings() {
    const token = app.globalData.token;
    if (!token) return;

    wx.request({
      url: `${app.globalData.baseUrl}/elder/1`,
      method: 'GET',
      header: { Authorization: `Bearer ${token}` },
      success: (res) => {
        if (res.statusCode === 200) {
          this.setData({
            privacyEnabled: res.data.privacy_enabled,
          });
        }
      },
    });
  },

  togglePrivacy(e) {
    const enabled = e.detail.value;
    const token = app.globalData.token;

    wx.request({
      url: `${app.globalData.baseUrl}/privacy/auth`,
      method: 'POST',
      header: { Authorization: `Bearer ${token}` },
      data: { elder_id: 1, enabled },
      success: (res) => {
        if (res.statusCode === 200) {
          this.setData({ privacyEnabled: enabled });
          wx.showToast({ title: '设置已更新' });
        }
      },
    });
  },

  toggleAlert(e) {
    this.setData({ alertEnabled: e.detail.value });
  },

  logout() {
    wx.removeStorageSync('token');
    app.globalData.token = '';
    wx.redirectTo({ url: '/pages/login/login' });
  },
});
