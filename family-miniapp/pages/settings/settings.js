const app = getApp();
const api = require('../../utils/api.js');

const emptyTask = () => ({ task_type: 'medication', title: '', content: '', schedule_time: '' });
const sessionScope = () => ({ token: app.globalData.token, session: app._bindingVersion,
  config: app._configEpoch, selected: app.globalData.currentElderId });
const sameScope = (left, right, includeSelection = true) => left && right
  && ['token', 'session', 'config'].every((key) => left[key] === right[key])
  && (!includeSelection || left.selected === right.selected);

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
    this._settingsVisible = true;
    this._settingsGeneration = (this._settingsGeneration || 0) + 1;
    this.loadSettings();
  },

  onHide() {
    this._settingsVisible = false;
    this._settingsGeneration = (this._settingsGeneration || 0) + 1;
  },

  onUnload() { this.onHide(); },

  /** Reuse the application's session/config versions; keep ownership local to this page. */
  _settingsContext() {
    const scope = sessionScope();
    if (!sameScope(this._settingsScope, scope)) {
      const sameSession = sameScope(this._settingsScope, scope, false);
      this._settingsScope = scope;
      this._loadedScope = null;
      this._settingsGeneration = (this._settingsGeneration || 0) + 1;
      this._settingsRequests = {};
      if (!sameSession) this.setData({ elderId: null, privacyEnabled: false, taskTypeIndex: 0, taskForm: emptyTask() });
    }
    return scope;
  },

  /** Reads and privacy writes share one ordering lane; task submissions own their draft. */
  _settingsRequest(kind, bound = false) {
    if (this._settingsVisible === false) return null;
    const scope = this._settingsContext();
    if (!scope.token || (bound && !sameScope(this._loadedScope, scope))) return null;
    const requests = this._settingsRequests;
    const sequence = requests[kind] = (requests[kind] || 0) + 1;
    return { scope, kind, sequence, generation: this._settingsGeneration,
      bound, elderId: this.data.elderId, draftVersion: this._taskDraftVersion,
      form: kind === 'task' ? { ...this.data.taskForm } : null };
  },

  _settingsCurrent(request, includeSelection = true) {
    return request && this._settingsVisible !== false
      && request.generation === this._settingsGeneration
      && request.sequence === this._settingsRequests[request.kind]
      && sameScope(request.scope, sessionScope(), includeSelection)
      && (!request.bound || request.elderId === this.data.elderId)
      && (!request.form || (request.draftVersion === this._taskDraftVersion
        && Object.keys(request.form).every((key) => request.form[key] === this.data.taskForm[key])));
  },

  /** 获取绑定老人（首条），同步隐私授权状态 */
  loadSettings() {
    const request = this._settingsRequest('settings');
    if (!request) return;
    api.get('/elder').then((res) => {
      if (!this._settingsCurrent(request)) {
        // Binding resolution can finish while this GET is pending; never apply its old result.
        if (this._settingsCurrent(request, false)) this.loadSettings();
        return;
      }
      this._loadedScope = request.scope;
      const list = res;
      if (list.length === 0) {
        this.setData({ elderId: null, privacyEnabled: false });
        return;
      }
      const first = list[0];
      this.setData({ elderId: first.id, privacyEnabled: !!first.privacy_enabled });
    }).catch(() => {});
  },

  /** 隐私授权开关（FR-CM-05） */
  togglePrivacy(e) {
    const enabled = e.detail.value;
    if (this.data.elderId == null) return;
    const request = this._settingsRequest('settings', true);
    if (!request || this.data.elderId == null) return;
    api.post('/privacy/auth', { elder_id: this.data.elderId, enabled }).then(() => {
      if (!this._settingsCurrent(request)) return;
      this.setData({ privacyEnabled: enabled });
      if (this._settingsCurrent(request)) wx.showToast({ title: '设置已更新' });
    }).catch(() => {});
  },

  /** 通知开关（FR-FL-07，P1 本地状态） */
  toggleAlert(e) {
    this.setData({ alertEnabled: e.detail.value });
  },

  /** 任务表单输入 */
  onTaskInput(e) {
    this._taskDraftVersion = (this._taskDraftVersion || 0) + 1;
    const field = e.currentTarget.dataset.field;
    const f = Object.assign({}, this.data.taskForm);
    f[field] = e.detail.value;
    this.setData({ taskForm: f });
  },

  /** 任务类型选择（medication / rest / sedentary） */
  onTaskTypeChange(e) {
    this._taskDraftVersion = (this._taskDraftVersion || 0) + 1;
    const idx = Number(e.detail.value) || 0;
    const types = ['medication', 'rest', 'sedentary'];
    const f = Object.assign({}, this.data.taskForm);
    f.task_type = types[idx];
    this.setData({ taskTypeIndex: idx, taskForm: f });
  },

  /** 新增提醒任务（FR-FL-05） */
  addTask() {
    const request = this._settingsRequest('task', true);
    if (!request) return;
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
      if (!this._settingsCurrent(request)) return;
      wx.showToast({ title: '任务已添加' });
      if (this._settingsCurrent(request)) this.setData({ taskForm: emptyTask() });
    }).catch(() => {});
  },

  logout() {
    this.onHide();
    this._settingsScope = null;
    this._loadedScope = null;
    this.setData({ elderId: null, privacyEnabled: false, taskTypeIndex: 0, taskForm: emptyTask() });
    if (app.clearAlertSession) app.clearAlertSession();
    wx.removeStorageSync('token');
    app.globalData.token = '';
    wx.redirectTo({ url: '/pages/login/login' });
  },
});
