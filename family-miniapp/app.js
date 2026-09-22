App({
  globalData: {
    baseUrl: 'https://your-server.com/api/v1',
    token: '',
    userInfo: null,
  },
  
  onLaunch() {
    const token = wx.getStorageSync('token');
    if (token) {
      this.globalData.token = token;
    }
  },
  
  login(callback) {
    wx.login({
      success: (res) => {
        if (res.code) {
          wx.request({
            url: `${this.globalData.baseUrl}/auth/login`,
            method: 'POST',
            data: { code: res.code },
            success: (response) => {
              if (response.data.code === 200) {
                this.globalData.token = response.data.data.token;
                wx.setStorageSync('token', response.data.data.token);
                callback && callback(true);
              } else {
                callback && callback(false);
              }
            },
          });
        }
      },
    });
  },
});
