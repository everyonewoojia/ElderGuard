'use strict';

/** Pages own subscriptions, while the application owns the native socket. */
function show(page, refresh) {
  hide(page);
  page._alertVisible = true;
  const app = getApp();
  if (app.watchAlerts) {
    page._alertUnsubscribe = app.watchAlerts((event) => {
      if (!page._alertVisible) return;
      if (event.type === 'reset') {
        invalidate(page);
        page.setData({ alerts: [] });
        if ('elderId' in page.data) page.setData({ elder: null, elderId: null, tasks: [], stats: { ...page.data.stats, medication: '0项' } });
      } else {
        if (event.type === 'context') { invalidate(page); page.setData({ alerts: [] }); }
        refresh(event);
      }
    });
  }
}

function invalidate(page) {
  page._alertVersion = (page._alertVersion || 0) + 1;
  page._alertRequest = null;
}

function hide(page) {
  page._alertVisible = false;
  invalidate(page);
  if (page._alertUnsubscribe) page._alertUnsubscribe();
  page._alertUnsubscribe = null;
}

/** Coalesce refresh signals and reject responses from old account/view scopes. */
function refresh(page, limit) {
  const app = getApp();
  const token = app.globalData.token;
  if (!token || page._alertVisible === false) return;
  const elder = app.globalData.currentElderId;
  const session = app._bindingVersion, config = app._configEpoch;
  const version = page._alertVersion;
  const valid = () => app.globalData.token === token && app.globalData.currentElderId === elder
    && app._bindingVersion === session && app._configEpoch === config
    && page._alertVersion === version && page._alertVisible !== false && page._alertRequest === request;
  const pending = page._alertRequest;
  if (pending && pending.token === token && pending.elder === elder && pending.version === version
      && pending.session === session && pending.config === config) {
    pending.again = true; return;
  }
  const request = { token, elder, version, session, config, again: false };
  page._alertRequest = request;
  const api = require('./api');
  api.get('/alerts').then((rows) => {
    if (!valid()) return;
    // P1-01: consume a bare array. No synthetic success for failed requests.
    const seen = new Set();
    const alerts = rows.filter((row) => {
      if (app.alertSocket && row.elder_id !== elder) return false;
      if (seen.has(row.id)) return false;
      seen.add(row.id); return true;
    });
    page.setData({ alerts: limit ? alerts.slice(0, limit) : alerts });
  }).catch(() => {}).finally(() => {
    const again = request.again && valid();
    if (page._alertRequest === request) page._alertRequest = null;
    if (again) refresh(page, limit);
  });
}

module.exports = { show, hide, refresh };
