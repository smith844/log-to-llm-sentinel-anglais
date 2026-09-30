// Run with: node --test tests/test_i18n.js
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function page(serverLanguage = 'en', cachedLanguage = 'fr') {
    let server = serverLanguage;
    let failSave = false;
    const requests = [], alerts = [], callbacks = {};
    const storage = new Map([['sentinel_lang', cachedLanguage]]);
    const context = {
        window: {}, console: {warn() {}, error() {}}, Date,
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        document: {
            getElementById: () => null, querySelectorAll: () => [],
            documentElement: {setAttribute() {}},
            addEventListener: (event, fn) => {callbacks[event] = fn;}
        },
        alert: message => alerts.push(message),
        fetch: async (url, options = {}) => {
            requests.push([url, options.method || 'GET']);
            if (url === '/api/i18n/languages') return {json: async () => []};
            if (url === '/api/config/site-lang') {
                if (options.method === 'PUT') {
                    if (failSave) return {ok: false};
                    server = JSON.parse(options.body).lang;
                }
                return {ok: true, json: async () => ({site_lang: server})};
            }
            return {ok: true, json: async () => ({common: {language_sync_error: 'Could not save'}})};
        }
    };
    vm.runInNewContext(fs.readFileSync('static/js/i18n.js', 'utf8'), context);
    return {context, requests, alerts, storage, init: callbacks.DOMContentLoaded,
        server: () => server, fail: () => {failSave = true;}};
}

test('page load uses saved server language without overwriting it from browser cache', async () => {
    const p = page('fr', 'en');
    await p.init();
    assert.equal(p.context.window.i18n.getCurrentLang(), 'fr');
    assert.equal(p.storage.get('sentinel_lang'), 'fr');
    assert.equal(p.requests.filter(([, method]) => method === 'PUT').length, 0);
});

test('rapid header switches keep server, UI and browser cache consistent', async () => {
    const p = page();
    await p.init();
    await Promise.all(['fr', 'en', 'fr'].map(lang => p.context.window.switchLanguage(lang)));
    assert.equal(p.server(), 'fr');
    assert.equal(p.context.window.i18n.getCurrentLang(), 'fr');
    assert.equal(p.storage.get('sentinel_lang'), 'fr');
});

test('failed save keeps the previous selection and reports the failure', async () => {
    const p = page();
    await p.init();
    p.fail();
    await p.context.window.switchLanguage('fr');
    assert.equal(p.server(), 'en');
    assert.equal(p.context.window.i18n.getCurrentLang(), 'en');
    assert.equal(p.storage.get('sentinel_lang'), 'en');
    assert.deepEqual(p.alerts, ['Could not save']);
});
