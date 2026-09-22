const app = getApp();

const request = (url, method, data) => {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${app.globalData.baseUrl}${url}`,
      method,
      data,
      header: {
        Authorization: `Bearer ${app.globalData.token}`,
      },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else if (res.statusCode === 401) {
          wx.redirectTo({ url: '/pages/login/login' });
          reject(new Error('未授权'));
        } else {
          reject(new Error(res.data.message || '请求失败'));
        }
      },
      fail: (err) => {
        reject(err);
      },
    });
  });
};

const get = (url) => request(url, 'GET');
const post = (url, data) => request(url, 'POST', data);
const put = (url, data) => request(url, 'PUT', data);
const del = (url) => request(url, 'DELETE');

module.exports = {
  request,
  get,
  post,
  put,
  del,
};
