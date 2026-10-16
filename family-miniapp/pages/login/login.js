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

    // 后端 /auth/login 使用 OAuth2PasswordRequestForm（表单编码），
    // 此处以 application/x-www-form-urlencoded 提交以对齐契约。
    wx.request({
      url: `${app.globalData.baseUrl}/auth/login`,
      method: 'POST',
      header: { 'content-type': 'application/x-www-form-urlencoded' },
      data: `username=${encodeURIComponent(phone)}&password=${encodeURIComponent(password)}`,
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
