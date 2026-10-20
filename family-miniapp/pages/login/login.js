const app = getApp();
const api = require('../../utils/api');

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

  onUnload() { api.cancelLogin(this._loginAttempt); },

  login() {
    const { phone, password } = this.data;

    if (!phone || !password) {
      wx.showToast({ title: '请输入手机号和密码', icon: 'none' });
      return;
    }

    let attempt;
    try { attempt = api.beginLoginAttempt(); }
    catch (_) { wx.showToast({ title: '服务地址未配置或不安全', icon: 'none' }); return; }
    this._loginAttempt = attempt;

    // 后端 /auth/login 使用 OAuth2PasswordRequestForm（表单编码），
    // 此处以 application/x-www-form-urlencoded 提交以对齐契约。
    wx.request({
      url: `${attempt.baseUrl}/auth/login`,
      method: 'POST',
      header: { 'content-type': 'application/x-www-form-urlencoded' },
      data: `username=${encodeURIComponent(phone)}&password=${encodeURIComponent(password)}`,
      success: (res) => {
        if (!api.isLoginCurrent(attempt)) return;
        if (!(res.statusCode === 200 && res.data
            && api.acceptLogin(attempt, res.data.access_token,
              () => wx.switchTab({ url: '/pages/index/index' })))) {
          if (!api.isLoginCurrent(attempt)) return;
          api.cancelLogin(attempt);
          wx.showToast({ title: '登录失败', icon: 'none' });
        }
      },
      fail: () => {
        if (!api.isLoginCurrent(attempt)) return;
        api.cancelLogin(attempt);
        wx.showToast({ title: '网络错误', icon: 'none' });
      },
    });
  },
});
