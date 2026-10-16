const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    elder: null,
    elderId: null,
    tasks: [],
    stats: {
      medication: '0项',
      activity: '-',
      sedentary: '-',
    },
    alerts: [],
  },

  onLoad() {
    this.loadBindElder();
  },

  /**
   * 获取当前家属绑定的老人（仅本人数据，AQ01）。
   * 未登录/未绑定 → 跳转登录页（未绑定不可查看）。
   */
  loadBindElder() {
    const token = app.globalData.token;
    if (!token) {
      wx.redirectTo({ url: '/pages/login/login' });
      return;
    }
    api.get('/elder').then((res) => {
      const list = res.data || [];
      if (list.length === 0) {
        this.setData({ elder: null, elderId: null });
        return;
      }
      const first = list[0];
      this.setData({ elder: first, elderId: first.id });
      this.loadTasks(first.id);
      this.loadAlerts();
    }).catch(() => {});
  },

  /** 今日提醒任务（FR-FL-02：老人提醒完成情况） */
  loadTasks(elderId) {
    api.get('/tasks').then((res) => {
      const all = res.data || [];
      const tasks = all.filter((t) => t.elder_id === elderId);
      const active = tasks.filter((t) => t.is_active);
      this.setData({
        tasks,
        stats: {
          medication: `${active.filter((t) => t.task_type === 'medication').length}项`,
          activity: '-',
          sedentary: '-',
        },
      });
    }).catch(() => {});
  },

  /** 最近告警（FR-FL-03） */
  loadAlerts() {
    api.get('/alerts').then((res) => {
      this.setData({ alerts: (res.data || []).slice(0, 3) });
    }).catch(() => {});
  },

  goToDetail() {
    wx.navigateTo({ url: '/pages/elder/detail' });
  },
});
