const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    elderId: null,
    privacyEnabled: false,
    alertEnabled: true,
    taskTypeIndex: 0,
    taskTypeLabels: ['用药', '作息', '久坐提醒'],
    taskForm: {
      task_type: 'medication',
      title: '',
      content: '',
      schedule_time: '',
    },
  },

  onShow() {
    this.loadSettings();
  },

  /** 获取绑定老人（首条），同步隐私授权状态 */
  loadSettings() {
    const token = app.globalData.token;
    if (!token) return;
    api.get('/elder').then((res) => {
      const list = res.data || [];
      if (list.length === 0) return;
      const first = list[0];
      this.setData({ elderId: first.id, privacyEnabled: !!first.privacy_enabled });
    }).catch(() => {});
  },

  /** 隐私授权开关（FR-CM-05） */
  togglePrivacy(e) {
    const enabled = e.detail.value;
    if (this.data.elderId == null) return;
    api.post('/privacy/auth', { elder_id: this.data.elderId, enabled }).then(() => {
      this.setData({ privacyEnabled: enabled });
      wx.showToast({ title: '设置已更新' });
    }).catch(() => {});
  },

  /** 通知开关（FR-FL-07，P1 本地状态） */
  toggleAlert(e) {
    this.setData({ alertEnabled: e.detail.value });
  },

  /** 任务表单输入 */
  onTaskInput(e) {
    const field = e.currentTarget.dataset.field;
    const f = Object.assign({}, this.data.taskForm);
    f[field] = e.detail.value;
    this.setData({ taskForm: f });
  },

  /** 任务类型选择（medication / rest / sedentary） */
  onTaskTypeChange(e) {
    const idx = Number(e.detail.value) || 0;
    const types = ['medication', 'rest', 'sedentary'];
    const f = Object.assign({}, this.data.taskForm);
    f.task_type = types[idx];
    this.setData({ taskTypeIndex: idx, taskForm: f });
  },

  /** 新增提醒任务（FR-FL-05） */
  addTask() {
    const { task_type, title, content, schedule_time } = this.data.taskForm;
    if (this.data.elderId == null || !title) {
      wx.showToast({ title: '请先绑定老人并填写任务标题', icon: 'none' });
      return;
    }
    api.post('/tasks', {
      elder_id: this.data.elderId,
      task_type,
      title,
      content,
      schedule_time,
    }).then(() => {
      wx.showToast({ title: '任务已添加' });
      this.setData({ taskForm: { task_type: 'medication', title: '', content: '', schedule_time: '' } });
    }).catch(() => {});
  },

  logout() {
    wx.removeStorageSync('token');
    app.globalData.token = '';
    wx.redirectTo({ url: '/pages/login/login' });
  },
});
