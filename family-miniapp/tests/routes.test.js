'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { runtime } = require('./helpers/runtime');
const root = path.resolve(__dirname, '..');
const config = JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8'));

/** Traverse source files without requiring dependencies or a WeChat installation. */
function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(file) : [file];
  });
}

for (const route of config.pages) {
  test(`route ${route}: four files, valid config, loadable Page and template handlers`, () => {
    for (const extension of ['js', 'json', 'wxml', 'wxss']) {
      assert.ok(fs.existsSync(path.join(root, `${route}.${extension}`)), `${route}.${extension} 缺失`);
    }
    const pageConfig = JSON.parse(fs.readFileSync(path.join(root, `${route}.json`), 'utf8'));
    assert.equal(typeof pageConfig, 'object');
    assert.equal(pageConfig.component, undefined);
    const { page } = runtime(route.replace(/^pages\//, ''), {});
    assert.ok(page && page.data, '入口必须注册 Page');
    const template = fs.readFileSync(path.join(root, `${route}.wxml`), 'utf8');
    assert.ok(template.trim());
    for (const match of template.matchAll(/(?:bind|catch)(?::)?(?:tap|change|input)="([A-Za-z_$][\w$]*)"/g)) {
      assert.equal(typeof page[match[1]], 'function', `模板处理器 ${match[1]} 必须存在`);
    }
  });
}

test('all navigation targets exist and tab/non-tab navigation uses the right API', () => {
  const tabs = new Set(config.tabBar.list.map((tab) => tab.pagePath));
  for (const tab of tabs) assert.ok(config.pages.includes(tab));
  const files = [...sourceFiles(path.join(root, 'pages')), ...sourceFiles(path.join(root, 'utils')), path.join(root, 'app.js')];
  let count = 0;
  for (const file of files.filter((file) => file.endsWith('.js'))) {
    const source = fs.readFileSync(file, 'utf8');
    for (const match of source.matchAll(/wx\.(navigateTo|redirectTo|switchTab|reLaunch)\(\{\s*url:\s*['"]([^'"]+)['"]/g)) {
      count++;
      const target = match[2].split('?')[0].replace(/^\//, '');
      assert.ok(config.pages.includes(target), `${file}: 未声明 ${target}`);
      assert.ok(fs.existsSync(path.join(root, `${target}.js`)), `${target} 无入口`);
      assert.ok(fs.existsSync(path.join(root, `${target}.wxml`)), `${target} 无模板`);
      if (match[1] === 'switchTab') assert.ok(tabs.has(target));
      if (['navigateTo', 'redirectTo'].includes(match[1])) assert.ok(!tabs.has(target));
    }
  }
  assert.ok(count >= 4, '必须实际检查已有导航调用');
});

test('home detail navigation preserves its existing parameter-free route', () => {
  const { page, wx } = runtime('index', {});
  let navigation;
  wx.navigateTo = (options) => { navigation = options; };
  page.goToDetail();
  assert.deepEqual(navigation, { url: '/pages/elder/detail' });
  assert.ok(fs.existsSync(path.join(root, 'pages/elder/detail.js')));
});
