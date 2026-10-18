// Execute the production request/version helpers without a browser or dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/app.js'), 'utf8');
const versions = source.slice(source.indexOf('function normalizeVersion'), source.indexOf('async function checkForAppUpdate'));
const requests = source.slice(source.indexOf('const apiRequests'), source.indexOf('// Keep unchanged cards'));

async function verifyChannelSwitchSubmission() {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      checked: false, value: '24', textContent: '', listeners: {},
      addEventListener(name, callback) { this.listeners[name] = callback; },
      requestSubmit() { return this.listeners.submit({ preventDefault() {} }); },
    });
    return elements.get(selector);
  };
  let reply;
  let submitted;
  const watched = [];
  const context = vm.createContext({
    $: element,
    api: async (url, options) => { assert.equal(url, '/api/update/settings'); submitted = JSON.parse(options.body); return reply; },
    renderUpdateStatus() {}, watchUpdateJob: job => watched.push(job),
    checkUpdateNow() {}, applyUpdateNow() {}, closeUpdateOverlay() {},
  });
  const start = source.indexOf("$('#update-settings-form')?.addEventListener('submit'");
  const end = source.indexOf("document.addEventListener('submit'", start);
  vm.runInContext(source.slice(start, end), context);
  reply = { settings: { experience_program: false }, activation: { id: 'stable', status: 'scheduled' } };
  await element('#update-experience').listeners.change();
  assert.equal(submitted.experience_program, false);
  assert.match(element('#update-settings-message').textContent, /已退出体验计划.*安装正式版/);
  assert.equal(watched[0].id, 'stable');
  reply = { settings: { experience_program: false }, activation_error: '正在刮削' };
  await element('#update-settings-form').requestSubmit();
  assert.match(element('#update-settings-message').textContent, /已退出体验计划.*安装正式版失败.*正在刮削/);
  assert.equal(watched.length, 1);
  element('#update-experience').checked = true;
  reply = { settings: { experience_program: true }, activation: { id: 'beta', status: 'scheduled' } };
  await element('#update-experience').listeners.change();
  assert.equal(submitted.experience_program, true);
  assert.match(element('#update-settings-message').textContent, /已加入体验计划.*安装体验版/);
}

async function verifyReportedVersion() {
  const tag = { dataset: {}, textContent: '' };
  let result = { checked_at: '2026-10-11T08:00:00Z', current: 'bata.20261010195006', target: 'bata.20261010195006', available: false, channel: 'bata' };
  const context = vm.createContext({ $: () => tag, api: async () => ({ result }) });
  vm.runInContext(source.slice(source.indexOf('function normalizeVersion'), source.indexOf('const UPDATE_OVERLAY_KEY')), context);
  const runtime = { version: 'bata.20261010195006', app_version: '1.1.38' };
  await context.checkForAppUpdate(runtime);
  assert.equal(tag.dataset.updateStatus, 'current', 'trust the completed server check for timestamped beta versions');
  result = { ...result, available: true, target: 'bata.20261011195006' };
  await context.checkForAppUpdate(runtime);
  assert.equal(tag.dataset.updateStatus, 'available');
  result = { ...result, error: '检查失败', available: false };
  await context.checkForAppUpdate(runtime);
  assert.equal(tag.dataset.updateStatus, 'unknown', 'failed checks must not claim the installation is current');
}

async function verifyUpdateOverlay() {
  const markup = fs.readFileSync(path.join(__dirname, '../javsp_web/web/index.html'), 'utf8');
  assert.match(markup, /id="update-overlay"[^>]*role="alertdialog"/);
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, { hidden: true, dataset: {}, textContent: '', focus() {} });
    return elements.get(selector);
  };
  const classes = new Set();
  const pending = new Map();
  const callbacks = [];
  const replies = [];
  let reloads = 0;
  const context = vm.createContext({
    $: element,
    api: async () => {
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      return reply;
    },
    document: { body: { classList: { add: name => classes.add(name), remove: name => classes.delete(name) } } },
    sessionStorage: { setItem: (key, value) => pending.set(key, value), getItem: key => pending.get(key), removeItem: key => pending.delete(key) },
    window: { setTimeout: callback => { callbacks.push(callback); return callbacks.length; }, clearTimeout() {} },
    location: { reload: () => reloads++ },
  });
  const updateFunctions = source.slice(source.indexOf('const UPDATE_OVERLAY_KEY'), source.indexOf('function scheduleGitHubStarInvite'));
  vm.runInContext(versions + '\n' + updateFunctions, context);
  context.renderUpdateStatus({ current: '1.2.0', channel: 'stable', settings: { experience_program: false }, result: { channel: 'bata' } });
  assert.equal(element('#update-channel-badge').textContent, '正式版', 'stale beta check results must not override the selected channel');
  context.renderUpdateStatus({ current: 'bata.20261011000000', channel: 'bata', settings: { experience_program: true } });
  assert.equal(element('#update-experience').disabled, false, 'a running beta must allow leaving the experience program');
  assert.match(element('#update-channel-help').textContent, /安装当前正式版/);
  context.renderUpdateStatus({ current: 'bata.20261011000000', channel: 'stable', settings: { experience_program: false } });
  assert.equal(element('#update-experience').checked, false);
  assert.equal(element('#update-channel-badge').textContent, '正式版');
  context.watchUpdateJob({ id: 'first' });
  assert.equal(element('#update-overlay').hidden, false);
  assert.ok(classes.has('update-in-progress'));
  assert.equal(JSON.parse(pending.get('javsp-web.update-job')).id, 'first');
  replies.push({ job: { id: 'first', status: 'downloading', message: '正在下载应用包' } });
  await context.pollUpdateOverlay();
  assert.equal(element('#update-overlay-status').textContent, '正在下载应用包');
  replies.push(new Error('server restarting'));
  await context.pollUpdateOverlay();
  assert.match(element('#update-overlay-status').textContent, /重新连接/);
  replies.push({ job: { id: 'first', status: 'updated' } });
  await context.pollUpdateOverlay();
  assert.equal(element('#update-overlay').dataset.phase, 'updated');
  assert.equal(pending.size, 0);
  callbacks.at(-1)();
  assert.equal(reloads, 1);
  context.watchUpdateJob({ id: 'second' });
  replies.push({ job: { id: 'second', status: 'rolled_back', message: '已恢复旧版本' } });
  await context.pollUpdateOverlay();
  assert.equal(element('#update-overlay').dataset.phase, 'failed');
  assert.equal(element('#update-overlay-close').hidden, false);
  context.closeUpdateOverlay();
  assert.equal(element('#update-overlay').hidden, true);
  assert.equal(classes.has('update-in-progress'), false);
  replies.push({ status: 'scheduled', id: 'manual' }, { settings: {}, result: {}, capability: { supported: true }, job: { id: 'manual', status: 'scheduled' } });
  const manual = context.applyUpdateNow();
  assert.equal(element('#update-overlay').hidden, false, 'manual update must show feedback before the request completes');
  await manual;
  assert.equal(JSON.parse(pending.get('javsp-web.update-job')).id, 'manual');
  context.closeUpdateOverlay();
  replies.push(new Error('当前还有运行中的任务'));
  await context.applyUpdateNow();
  assert.equal(element('#update-overlay').hidden, true);
  assert.equal(element('#update-settings-message').textContent, '当前还有运行中的任务');
}

async function verifyBatchTaskStop() {
  // 批量停止：一个请求带上要停止的 ID；不带 ID 表示停止全部
  const requests = [];
  const toasts = [];
  let confirmRun;
  const context = vm.createContext({
    state: { tasks: [], selectedOverviewTasks: new Set(['a']) },
    $: () => ({ textContent: '' }),
    api: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return { ok: true, cancelled: 2 }; },
    confirmAction: ({ run }) => { confirmRun = run; },
    showToast: message => toasts.push(message),
    loadTasks: async () => {},
  });
  vm.runInContext(source.slice(source.indexOf('function stopTasks('), source.indexOf('async function deleteTaskInDialog(')), context);
  context.stopAllTasks();
  await confirmRun();
  assert.deepEqual(requests[0], { url: '/api/tasks/cancel', body: { ids: [] } }, 'stop-all must ask the server to stop everything');
  assert.equal(context.state.selectedOverviewTasks.size, 1, 'stop-all must not drop unrelated selections');
  assert.match(toasts[0], /已停止 2 个任务/);
  context.stopTasks(['a', 'a', 'b']);
  await confirmRun();
  assert.deepEqual(requests[1].body, { ids: ['a', 'b'] }, 'selected stops must send unique IDs');
  assert.equal(context.state.selectedOverviewTasks.size, 0, 'stopped selections must be cleared');
  // 概览已改为使用看板：批量停止入口仍在（手动刮削在 app.js 生成，概览在 index.html）
  const markup = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/app.js'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../javsp_web/web/index.html'), 'utf8');
  assert.ok(markup.includes('id="task-stop-all"'), 'missing stop control id="task-stop-all"');
  assert.ok(page.includes('id="overview-stop-all"'), 'missing stop control id="overview-stop-all"');
}

async function verifyRetryControls() {
  const root = path.join(__dirname, '..');
  const app = fs.readFileSync(path.join(root, 'javsp_web/web/assets/app.js'), 'utf8');
  const library = fs.readFileSync(path.join(root, 'javsp_web/web/assets/library.js'), 'utf8');
  assert.match(app, /data-retry-task=/, '失败任务卡片要有重新刮削按钮');
  assert.match(app, /data-task-metadata=/, '任务卡片要有资料按钮');
  assert.match(app, /id="task-retry-failed"/, '工具栏要有批量重试失败');
  assert.match(app, /message: '重试失败任务'|title: '重试失败任务'/, '批量重试需要确认框');
  assert.match(library, /编辑番号等资料">资料</, '媒体库的详情按钮改叫资料');

  const calls = [];
  const context = vm.createContext({
    state: {},
    $: () => ({ close() {} }),
    api: async (url, options) => { calls.push({ url, method: options?.method }); return { task_id: 'new-1' }; },
    showToast: (message) => calls.push({ toast: message }),
    loadTasks: async () => { calls.push({ refreshed: true }); },
    confirmAction: ({ run }) => run(),
  });
  const start = app.indexOf('async function retryTask(');
  const end = app.indexOf('async function deleteTaskInDialog(');
  vm.runInContext(app.slice(start, end), context);
  await context.retryTask('task-1');
  assert.deepEqual(calls[0], { url: '/api/tasks/task-1/retry', method: 'POST' });
  assert.ok(calls.some((call) => call.refreshed), '重试后要刷新列表');
  calls.length = 0;
  await context.retryFailedTasks();
  await new Promise((resolve) => setTimeout(resolve, 25));  // confirmAction 的回调是异步执行的
  assert.ok(calls.some((call) => call.url === '/api/tasks/retry-failed' && call.method === 'POST'));
  assert.ok(calls.some((call) => call.toast && /重新加入/.test(call.toast)));
}

async function main() {
  let count = 0;
  let release;
  let abortTimer;
  let mode = 'pending';
  const context = vm.createContext({
    state: {}, location: {}, AbortController,
    window: { setTimeout: fn => { abortTimer = fn; return 1; }, clearTimeout: () => {} },
    formatApiError: (_, status) => `HTTP ${status}`,
    fetch: async (url, options) => {
      count++;
      if (mode === 'pending') await new Promise(resolve => { release = resolve; });
      if (mode === 'abort') return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(Object.assign(new Error(), { name: 'AbortError' }))));
      if (mode === 'cached') { assert.equal(options.headers['If-None-Match'], 'v1'); return { status: 304 }; }
      if (mode === 'error') return { status: 503, ok: false, json: async () => ({}) };
      return { status: 200, ok: true, headers: { get: () => 'v1' }, json: async () => ({ items: [{ id: 1 }], total: 1 }) };
    },
  });
  vm.runInContext(versions + '\n' + requests, context);
  assert.equal(context.normalizeVersion('vv1.1.36'), '1.1.36');
  assert.equal(context.compareReleaseVersions('vv1.1.36', 'v1.1.36'), 0);
  assert.ok(context.compareReleaseVersions('1.1.36', 'v1.1.37') < 0);
  assert.equal(context.compareReleaseVersions('bata.20260923', '1.1.36'), null);
  const first = context.api('/api/tasks?limit=50');
  const second = context.api('/api/tasks?limit=50');
  assert.equal(count, 1, 'concurrent GETs must share a request');
  release();
  assert.equal(await first, await second);
  mode = 'cached';
  assert.equal((await context.api('/api/tasks?limit=50')).items[0].id, 1);
  mode = 'error';
  await assert.rejects(context.api('/api/failed'), /503/);
  mode = 'success';
  await context.api('/api/failed');
  mode = 'abort';
  const timed = context.api('/api/slow');
  abortTimer();
  await assert.rejects(timed, /请求超时/);
  const submitted = context.api('/api/tasks', { method: 'POST' });
  abortTimer();
  await assert.rejects(submitted, /先刷新列表确认/);
  mode = 'success';
  await context.api('/api/slow');
  await verifyUpdateOverlay();
  await verifyReportedVersion();
  await verifyChannelSwitchSubmission();
  await verifyBatchTaskStop();
  await verifyRetryControls();
  console.log('Web client regression checks passed: version normalization, shared GET, ETag, timeout/retry, update overlay, batch stop.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
