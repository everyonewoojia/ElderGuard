'use strict';
const { validateBaseUrl } = require('./api');

/** One application-owned connection; diagnostic snapshots never contain credentials. */
class AlertSocket {
  constructor({ wx, getSession, onAuthExpired, onTransportFailure, setTimer = setTimeout, clearTimer = clearTimeout }) {
    this.wx = wx;
    this.getSession = getSession;
    this.onAuthExpired = onAuthExpired;
    this.onTransportFailure = onTransportFailure;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.foreground = false;
    this.context = {};
    this.generation = 0;
    this.listeners = new Set();
    this.seen = new Set();
    this.state = 'idle';
    this.attempts = 0;
    this.socket = null;
    this.retry = null;
    this.deadline = null;
    this.stable = null;
    this.blockedSession = null;
  }

  status() {
    return { state: this.state, attempts: this.attempts, seen: this.seen.size, listeners: this.listeners.size };
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(type) {
    for (const listener of [...this.listeners]) {
      try { listener({ type }); } catch (_) { /* Isolate page callbacks. */ }
    }
  }

  clearTimers() {
    for (const key of ['retry', 'deadline', 'stable']) {
      if (this[key] !== null) this.clearTimer(this[key]);
      this[key] = null;
    }
  }

  retire() {
    ++this.generation;
    this.clearTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) this.closeTask(socket);
  }

  /** Native close can fail late, including a task returned after synchronous fail. */
  closeTask(socket) {
    const generation = this.generation;
    const failed = () => {
      if (this.generation !== generation) return;
      this.clearTimers(); this.state = 'cleanup_failed';
    };
    try { socket.close({ code: 1000, reason: 'client lifecycle', fail: failed }); }
    catch (_) { failed(); }
  }

  stop(reason = 'closed') {
    this.retire();
    this.listeners.clear();
    this.seen.clear();
    if (this.state !== 'cleanup_failed') this.state = reason;
  }

  setForeground(value) {
    const changed = this.foreground !== value;
    this.foreground = value;
    if (!value) {
      this.retire();
      if (this.state !== 'cleanup_failed' && this.state !== 'auth_failed') this.state = 'background';
    } else {
      if (changed) this.attempts = 0;
      this.sync();
    }
  }

  /** Every entry, including direct connect/retry, reaches this before native I/O. */
  prepare() {
    const next = this.getSession();
    if (next.token !== this.context.token || next.elderId !== this.context.elderId || next.baseUrl !== this.context.baseUrl
        || next.configuration?.epoch !== this.context.configuration?.epoch || next.session !== this.context.session) {
      this.retire();
      this.listeners.clear();
      this.seen.clear();
      this.attempts = 0;
      this.context = { ...next };
      if (this.state !== 'cleanup_failed') this.state = 'idle';
    }
    if (this.state === 'cleanup_failed') return null;
    const denied = (reason) => {
      this.retire();
      if (this.state !== 'cleanup_failed') this.state = reason;
      return null;
    };
    if (typeof next.token !== 'string' || !/^[A-Za-z0-9_.~-]+$/.test(next.token)
        || !Number.isSafeInteger(next.session) || next.session <= 0
        || !Number.isSafeInteger(next.elderId) || next.elderId <= 0 || next.bound !== true) {
      return denied('idle');
    }
    if (next.session === this.blockedSession) return denied('auth_failed');
    if (!next.configuration) return denied('unsupported_url');
    // Explicit deployment gate: mocks cannot certify a target WeChat runtime.
    if (next.verified !== true || typeof this.wx.connectSocket !== 'function') return denied('platform_unverified');
    let config;
    try { config = validateBaseUrl(next); } catch (_) { return denied('unsupported_url'); }
    if (!this.foreground) return denied('background');
    return config;
  }

  sync() { this.connect(); }

  current(generation) {
    if (generation !== this.generation) return false;
    // Revocation observed in a native callback must also retire its task/timers.
    const config = this.prepare();
    return Boolean(config) && generation === this.generation;
  }

  fail(generation) {
    if (!this.current(generation)) return;
    this.retire();
    if (this.state === 'cleanup_failed') return;
    if (this.attempts >= 5) this.state = 'exhausted';
    else {
      const delay = Math.min(16000, 1000 * 2 ** this.attempts++);
      this.state = 'reconnecting';
      this.retry = this.setTimer(() => { this.retry = null; this.sync(); }, delay);
    }
    // Before accept(), a server may reject the HTTP upgrade without a 4401 frame.
    if (this.onTransportFailure) this.onTransportFailure();
  }

  connect() {
    const config = this.prepare();
    if (!config || this.socket || this.retry !== null || this.state === 'exhausted') return;
    const generation = ++this.generation;
    this.state = 'connecting';
    let socket;
    try {
      socket = this.wx.connectSocket({
        url: config.socketUrl,
        header: { Authorization: `Bearer ${this.context.token}` },
        fail: () => this.fail(generation),
      });
      if (!this.current(generation)) {
        // A synchronous fail may precede the return of the SocketTask.
        this.closeTask(socket);
        return;
      }
      this.socket = socket;
      socket.onOpen(() => {
        if (!this.current(generation) || this.state === 'connected') return;
        this.clearTimers();
        this.state = 'connected';
        // Brief open/close flapping must not reset the retry budget forever.
        this.stable = this.setTimer(() => { this.stable = null; if (this.current(generation)) this.attempts = 0; }, 30000);
        this.emit('sync');
      });
      socket.onMessage((event) => { if (this.current(generation)) this.message(event.data); });
      socket.onError(() => this.fail(generation));
      socket.onClose((event) => {
        if (!this.current(generation)) return;
        if (event.code === 4401) {
          this.blockedSession = this.context.session;
          this.stop('auth_failed');
          this.onAuthExpired();
        } else this.fail(generation);
      });
      this.deadline = this.setTimer(() => this.fail(generation), 10000);
    } catch (_) { this.fail(generation); }
  }

  message(raw) {
    if (typeof raw !== 'string' || raw.length > 65536) return;
    let packet;
    try { packet = JSON.parse(raw); } catch (_) { return; }
    const data = packet && packet.type === 'alert' && packet.data;
    if (!data || Array.isArray(data) || !Number.isSafeInteger(data.id) || data.id <= 0
        || data.elder_id !== this.context.elderId
        || !['help', 'fall', 'abnormal'].includes(data.alert_type)
        || !['P0', 'P1', 'P2'].includes(data.priority)
        || !['pending', 'notified', 'cancelled'].includes(data.status)
        || !(data.content === null || typeof data.content === 'string')
        || !(data.created_at === null || (typeof data.created_at === 'string' && Number.isFinite(Date.parse(data.created_at))))) return;
    const key = `${data.elder_id}:${data.id}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    if (this.seen.size > 256) this.seen.delete(this.seen.values().next().value);
    this.emit('sync');
  }
}

module.exports = { AlertSocket };
