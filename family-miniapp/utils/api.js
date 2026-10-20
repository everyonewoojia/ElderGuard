function configError() {
  const error = new Error('服务地址未配置或不安全');
  error.code = 'API_CONFIG_INVALID';
  return error;
}

/** Strict component parser for the fixed API base; no browser URL global required. */
function validateBaseUrl(config) {
  const invalid = () => { throw configError(); };
  const { baseUrl, apiTrustedHosts } = config || {};
  if (typeof baseUrl !== 'string' || baseUrl.length > 2048
      || !baseUrl.startsWith('https://') || /[\s\\%@?#]/.test(baseUrl)) return invalid();
  const parts = baseUrl.slice(8).split('/');
  const authority = parts.shift().split(':');
  if (authority.length > 2 || (authority.length === 2 && authority[1] !== '443')) return invalid();
  const host = authority[0].toLowerCase();
  const hostValid = (value) => typeof value === 'string' && value.length <= 253
    && value.split('.').every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label));
  if (!hostValid(host) || /(^|\.)(your-server\.com|example\.(com|org|net))$/.test(host)
      || !Array.isArray(apiTrustedHosts) || !apiTrustedHosts.length
      || !apiTrustedHosts.every(hostValid)
      || !apiTrustedHosts.some((value) => value.toLowerCase() === host)) return invalid();
  const path = '/' + parts.join('/');
  if (path !== '/api/v1' && path !== '/api/v1/') return invalid();
  return { baseUrl: `https://${host}/api/v1`, socketUrl: `wss://${host}/api/v1/ws/alerts` };
}

/** Explicit, nonsecret configuration. Source labels describe review, not cryptographic approval. */
function validateEnvironmentConfig(expected, value) {
  const environments = ['development', 'test', 'production'];
  if (!environments.includes(expected) || !value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).some((key) => !['environment', 'source', 'baseUrl', 'apiTrustedHosts'].includes(key))
      || value.environment !== expected || !['reviewed-config', 'offline-test'].includes(value.source)
      || (expected === 'production' && value.source !== 'reviewed-config')) throw configError();
  const addresses = validateBaseUrl(value);
  const hosts = value.apiTrustedHosts.map((host) => host.toLowerCase()).sort();
  if (new Set(hosts).size !== hosts.length
      || hosts.some((host) => /(^|\.)(your-server\.com|example\.(com|org|net))$/.test(host))) throw configError();
  if (expected === 'production' && hosts.some((host) => !host.includes('.')
      || /^[0-9.]+$/.test(host) || /(^|\.)(localhost|invalid|test|example)$/.test(host))) throw configError();
  const scope = JSON.stringify([expected, value.source, addresses.baseUrl, hosts]);
  return Object.freeze({ ...addresses, environment: expected, source: value.source,
    apiTrustedHosts: Object.freeze(hosts), scope });
}

/** Reconfiguration revokes the old session even if the replacement is invalid. */
function configureEnvironment(app, expected, value) {
  const previous = app._configEpoch || 0;
  const epoch = app._configEpoch = previous + 1;
  app._environmentConfig = null;
  app._expectedEnvironment = expected;
  Object.assign(app.globalData, { baseUrl: '', apiTrustedHosts: [], apiEnvironment: '', apiConfigSource: '',
    alertSocketHeaderVerified: false });
  if (previous) {
    if (app.clearAlertSession) app.clearAlertSession();
    else { app._loginVersion = (app._loginVersion || 0) + 1; app.globalData.token = ''; }
  }
  const active = validateEnvironmentConfig(expected, value);
  if (epoch !== app._configEpoch) throw configError();
  app._environmentConfig = active;
  Object.assign(app.globalData, { baseUrl: active.baseUrl, apiTrustedHosts: [...active.apiTrustedHosts],
    apiEnvironment: active.environment, apiConfigSource: active.source });
  return active;
}

/** Mutable legacy mirrors never replace the installed, validated configuration. */
function getConfig(app = getApp()) {
  const active = app._environmentConfig;
  const data = app.globalData;
  if (!active || active.environment !== app._expectedEnvironment || data.apiEnvironment !== active.environment
      || data.apiConfigSource !== active.source) throw configError();
  const current = validateBaseUrl(data);
  const hosts = data.apiTrustedHosts.map((host) => host.toLowerCase()).sort();
  if (current.baseUrl !== active.baseUrl || JSON.stringify(hosts) !== JSON.stringify(active.apiTrustedHosts)) throw configError();
  return { ...active, epoch: app._configEpoch };
}

function contextCurrent(config) {
  try { const now = getConfig(); return now.epoch === config.epoch && now.scope === config.scope; }
  catch (_) { return false; }
}

function contextError() {
  const error = new Error('服务配置已变更，请重新操作');
  error.code = 'API_CONTEXT_CHANGED';
  return error;
}

/** Never retain an untrusted response body or native request/error object. */
function httpError(status) {
  const messages = { 400: '请求参数无效', 401: '未授权', 403: '无权访问该老人数据',
    404: '请求的资源不存在', 429: '请求过于频繁' };
  const error = new Error(messages[status] || (status >= 500 ? '服务暂时不可用' : '请求失败'));
  error.code = 'HTTP_ERROR';
  error.statusCode = status;
  return error;
}

function networkError(raw) {
  let message = '', status;
  try {
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      if (typeof raw.errMsg === 'string') message = raw.errMsg;
      status = raw.statusCode;
    }
  } catch (_) { /* Malformed native fields cannot escape the error boundary. */ }
  // Retain existing auth-probe handling for explicit numeric rejection statuses.
  if (Number.isInteger(status) && status >= 400 && status <= 599) return httpError(status);
  let code = 'NETWORK_FAILURE', text = '网络请求失败，请重试';
  if (/^request:fail timeout(?:\s|$)/.test(message)) { code = 'NETWORK_TIMEOUT'; text = '网络请求超时，请重试'; }
  else if (/^request:fail abort(?:\s|$)/.test(message)) { code = 'NETWORK_CANCELLED'; text = '网络请求已取消'; }
  else if (/^request:fail connect fail(?:\s|$)/.test(message)) { code = 'NETWORK_CONNECTION_FAILED'; text = '网络连接失败，请重试'; }
  const error = new Error(text);
  error.code = code;
  return error;
}

/** Login attempts are shared by the page and legacy entry, and invalidated on logout. */
function beginLoginAttempt() {
  const app = getApp();
  app._loginVersion = (app._loginVersion || 0) + 1;
  const config = getConfig(app);
  return { version: app._loginVersion, token: app.globalData.token, baseUrl: config.baseUrl,
    scope: config.scope, epoch: config.epoch };
}

function isLoginCurrent(attempt) {
  const app = getApp();
  if (!attempt || attempt.version !== app._loginVersion || attempt.token !== app.globalData.token) return false;
  return contextCurrent(attempt);
}

function cancelLogin(attempt) {
  const app = getApp();
  // Cleanup invalidates its own identity even when configuration has been revoked.
  if (attempt && attempt.version === app._loginVersion) ++app._loginVersion;
}

function acceptLogin(attempt, token, after) {
  if (!isLoginCurrent(attempt) || typeof token !== 'string' || !/^[A-Za-z0-9_.~-]+$/.test(token)) return false;
  const app = getApp();
  if (app.clearAlertSession) app.clearAlertSession();
  else ++app._loginVersion;
  // A reset listener may have started another attempt during synchronous cleanup.
  const cleared = { ...attempt, version: attempt.version + 1, token: '' };
  if (!isLoginCurrent(cleared)) return false;
  app.globalData.token = token;
  const accepted = { ...attempt, version: app._loginVersion, token };
  if (isLoginCurrent(accepted)) wx.setStorageSync('token', { value: token, scope: attempt.scope });
  if (isLoginCurrent(accepted) && app.refreshAlertBinding) app.refreshAlertBinding();
  if (isLoginCurrent(accepted) && after) after();
  return true;
}

const request = (url, method, data) => {
  const app = getApp();
  const requestToken = app.globalData.token;
  const requestSession = app._bindingVersion;
  return new Promise((resolve, reject) => {
    const config = getConfig(app);
    const failed = (raw) => reject(contextCurrent(config) ? networkError(raw) : contextError());
    try { wx.request({
      url: `${config.baseUrl}${url}`,
      method,
      data,
      header: {
        Authorization: `Bearer ${requestToken}`,
      },
      success: (res) => {
        if (!contextCurrent(config)) { reject(contextError()); return; }
        if (!res || !Number.isInteger(res.statusCode) || res.statusCode < 100 || res.statusCode > 599) {
          const error = new Error('服务响应无效，请重试');
          error.code = 'HTTP_RESPONSE_INVALID';
          reject(error); return;
        }
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else if (res.statusCode === 401) {
          let sessionExpired = false;
          if (app.globalData.token === requestToken && app._bindingVersion === requestSession) {
            sessionExpired = true;
            if (app.clearAlertSession) app.clearAlertSession({ expired: true });
            else wx.redirectTo({ url: '/pages/login/login' });
          }
          const error = httpError(401);
          // Internal UI hint: this response actually expired its own current session.
          error.sessionExpired = sessionExpired;
          reject(error);
        } else {
          reject(httpError(res.statusCode));
        }
      },
      fail: failed,
    }); } catch (error) { failed(error); }
  });
};

const get = (url) => request(url, 'GET');
const post = (url, data) => request(url, 'POST', data);
const put = (url, data) => request(url, 'PUT', data);
const del = (url) => request(url, 'DELETE');

module.exports = {
  validateBaseUrl,
  validateEnvironmentConfig,
  configureEnvironment,
  getConfig,
  beginLoginAttempt,
  isLoginCurrent,
  cancelLogin,
  acceptLogin,
  request,
  get,
  post,
  put,
  del,
};
