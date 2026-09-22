const app = getApp();

Page({
  data: {
    elder: {
      name: '加载中...',
      age: '',
      status: '在线',
    },
    stats: {
      medication: '0/3',
      activity: '5',
      sedentary: '2小时',
    },
    alerts: [],
  },

  onLoad() {
    this.loadElderInfo();
    this.loadAlerts();
  },

  loadElderInfo() {
    const token = app.globalData.token;
    if (!token) {
      wx.redirectTo({ url: '/pages/login/login' });
      return;
    }

    wx.request({
      url: `${app.globalData.baseUrl}/elder/1`,
      method: 'GET',
      header: { Authorization: `Bearer ${token}` },
      success: (res) => {
        if (res.statusCode === 200) {
          this.setData({ elder: res.data });
        }
      },
    });
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
          this.setData({ alerts: res.data.slice(0, 3) });
        }
      },
    });
  },

  goToDetail() {
    wx.navigateTo({ url: '/pages/elder/detail' });
  },
});
