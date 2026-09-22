const app = getApp();

Page({
  data: {
    phone: '',
    password: '',
  },

  onPhoneInput(e) {
    this.setData({ phone: e.detail.value });
  },

  onPasswordInput(e) {
    this.setData({ password: e.detail.value });
  },

  login() {
    const { phone, password } = this.data;
    
    if (!phone || !password) {
      wx.showToast({ title: '请输入手机号和密码', icon: 'none' });
      return;
    }

    wx.request({
      url: `${app.globalData.baseUrl}/auth/login`,
      method: 'POST',
      data: { username: phone, password },
      success: (res) => {
        if (res.statusCode === 200 && res.data.access_token) {
          app.globalData.token = res.data.access_token;
          wx.setStorageSync('token', res.data.access_token);
          wx.switchTab({ url: '/pages/index/index' });
        } else {
          wx.showToast({ title: '登录失败', icon: 'none' });
        }
      },
      fail: () => {
        wx.showToast({ title: '网络错误', icon: 'none' });
      },
    });
  },
});
