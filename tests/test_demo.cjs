const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { test } = require('node:test');

const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8').replaceAll('\r\n', '\n');
function demo() {
  const timers = [];
  const events = {};
  const context = vm.createContext({
    window: {}, URL, Response, crypto,
    location: { origin: 'https://example.test', href: 'https://example.test/JavSP-Web/demo.html' },
    document: { addEventListener: (name, callback) => { events[name] = callback; } },
    setTimeout: callback => timers.push(callback),
  });
  vm.runInContext(read('docs/demo-seed.js'), context);
  vm.runInContext(read('docs/demo-mock.js'), context);
  const request = (url, method = 'GET', body) => context.window.fetch(url, { method, body: JSON.stringify(body) });
  const json = async (...args) => (await request(...args)).json();
  return { context, timers, events, request, json };
}

test('demo uses the current application assets and all documentation links resolve', () => {
  for (const name of ['theme.css', 'theme.js', 'community.js', 'app.js', 'library.js', 'app.css', 'overrides.css', 'cinema.css', 'ai.js', 'ai-markdown.js']) {
    assert.equal(read(`docs/assets/${name === 'app.js' ? 'demo-app.js' : name}`), read(`javsp_web/web/assets/${name}`));
  }
  const pages = ['docs.html', ...fs.readdirSync(path.join(root, 'docs')).filter(file => /^guide-.*\.html$/.test(file))];
  assert.equal(pages.length, 6);
  for (const page of pages) {
    const document = read(`docs/${page}`);
    for (const [, anchor] of document.matchAll(/href="#([^"]+)"/g)) assert.ok(document.includes(`id="${anchor}"`), `${page}#${anchor}`);
    for (const [, file] of document.matchAll(/(?:src|href)="(assets\/[^"?#]+)"/g)) assert.ok(fs.existsSync(path.join(root, 'docs', file)), file);
    for (const [, file, anchor] of document.matchAll(/href="([a-z-]+\.html)(?:#([a-z-]+))?"/g)) {
      const target = read(`docs/${file}`);
      if (anchor) assert.ok(target.includes(`id="${anchor}"`), `${page} -> ${file}#${anchor}`);
    }
  }
  assert.match(read('docs/demo.html'), /demo-seed.js.*\n<script src="demo-mock.js/);
});

test('media library demo exposes libraries, folders, statuses and manual editing', async () => {
  const { json, request } = demo();
  const libraries = (await json('/api/library/libraries')).libraries;
  assert.equal(libraries.length, 2);
  assert.equal(libraries[0].name, '全部影片');
  assert.equal(libraries[0].folders.length, 2);
  assert.equal(libraries[0].film_count, 5);
  const libraryId = libraries[0].id;

  const films = await json(`/api/library/films?limit=50&library_id=${libraryId}`);
  assert.equal(films.total, 5);
  assert.equal(films.stats.complete, 1);
  const byId = Object.fromEntries(films.items.map(item => [item.dvdid, item]));
  assert.equal(byId['DEMO-002'].status, 'partial');
  assert.equal(byId['DEMO-003'].status, 'unscraped');
  assert.equal(byId['DEMO-004'].scrape_state, 'failed');
  assert.equal(byId['DEMO-005'].status, 'missing');
  assert.equal((await json(`/api/library/films?status=partial&library_id=${libraryId}`)).total, 1);
  assert.equal((await json(`/api/library/films?query=DEMO-004&library_id=${libraryId}`)).items[0].status, 'failed');

  // 库内单个文件夹也能单独过滤
  const folderId = libraries[0].folders[0].id;
  assert.equal((await json(`/api/library/films?limit=50&folder_id=${folderId}`)).total, 3);
  // 空库没有影片
  assert.equal((await json(`/api/library/films?limit=50&library_id=${libraries[1].id}`)).total, 0);

  assert.deepEqual((await json(`/api/library/groups?by=actress&library_id=${libraryId}`)).by, 'actress');
  assert.equal((await json(`/api/library/groups?by=actress&library_id=${libraryId}`)).all.count, 5, '分组接口返回全部影片汇总');
  const actressGroup = (await json(`/api/library/groups?by=actress&library_id=${libraryId}`)).groups[0];
  const groupUrl = `/api/library/films?limit=50&library_id=${libraryId}&group_by=actress&group=${encodeURIComponent(actressGroup.key)}`;
  assert.equal((await json(groupUrl)).total, actressGroup.count);
  const studioGroup = (await json(`/api/library/groups?by=studio&library_id=${libraryId}`)).groups.find(group => group.key === '示例片商');
  const studioUrl = `/api/library/films?limit=50&library_id=${libraryId}&group_by=studio&group=${encodeURIComponent(studioGroup.key)}`;
  assert.equal((await json(studioUrl)).total, studioGroup.count);

  // 新建媒体库并追加第二个文件夹
  const created = await json('/api/library/libraries', 'POST', { name: '演示新库', folders: [{ path: '/video/新目录', recursive: true }], scan: false });
  assert.equal(created.library.name, '演示新库');
  assert.equal(created.library.folders.length, 1);
  const added = await json(`/api/library/libraries/${created.library.id}/folders`, 'POST', { path: '/video/另一个目录', scan: false });
  assert.equal(added.library.folders.length, 2);
  const removed = await json(`/api/library/libraries/${created.library.id}/folders/${added.folder.id}`, 'DELETE');
  assert.equal(removed.library.folders.length, 1);
  await json(`/api/library/libraries/${created.library.id}`, 'DELETE');
  assert.equal((await json('/api/library/libraries')).libraries.length, 2);

  const target = byId['DEMO-002'];
  const saved = await json(`/api/library/films/${target.id}`, 'PATCH', { dvdid: 'DEMO-002', title: '手动标题', actress: ['示例演员'], studio: '示例片商', release_date: '2026-10-01', runtime: 120 });
  assert.equal(saved.film.title, '手动标题');
  assert.ok(saved.film.nfo_mtime > 0);
  const nfo = await json(`/api/library/films/${target.id}/nfo`);
  assert.match(nfo.content, /手动标题/);
  const scraped = await json(`/api/library/films/${target.id}/scrape`, 'POST', { dvdid: 'DEMO-002' });
  assert.equal(scraped.film.scrape_state, 'queued');
  // 封面包图在静态示例中不提供，确认返回可预期的错误而不是崩溃
  assert.equal((await request(`/api/library/films/${target.id}/cover`)).status, 409);
});

test('task pagination, filtering and cancellation survive pending timers', async () => {
  const { json, timers } = demo();
  const page = await json('/api/tasks?limit=1');
  assert.equal(page.items.length, 1);
  assert.equal(page.total, 2);
  assert.equal(page.metrics.latest_status, 'succeeded');
  assert.equal((await json('/api/tasks?status=failed')).items[0].name, 'DEMO-002');
  assert.equal((await json('/api/tasks?query=missing')).total, 0);
  const created = await json('/api/tasks', 'POST', { input_directory: '/video/DEMO-003.mkv' });
  await json(`/api/tasks/${created.task_id}/cancel`, 'POST');
  timers.forEach(callback => callback());
  assert.equal((await json(`/api/tasks/${created.task_id}`)).status, 'cancelled');
  await json(`/api/tasks/${created.task_id}`, 'DELETE');
  assert.equal((await json('/api/tasks')).total, 2);
});

test('old chapter bookmarks redirect to the matching topic page', () => {
  const pages = fs.readdirSync(path.join(root, 'docs')).filter(file => /^guide-.*\.html$/.test(file));
  for (const page of pages) {
    for (const [, anchor] of read(`docs/${page}`).matchAll(/<section id="([^"]+)"/g)) {
      let destination;
      const context = vm.createContext({
        document: { querySelector: selector => selector === '.guide-directory' ? {} : null, querySelectorAll: () => [] },
        window: { addEventListener() {} },
        location: { hash: `#${anchor}`, replace: value => { destination = value; } },
      });
      vm.runInContext(read('docs/script.js'), context);
      assert.equal(destination, `${page}#${anchor}`);
    }
  }
});

test('AI conversation lifecycle and unsupported actions', async () => {
  const { json, request } = demo();
  const conversation = await json('/api/ai/chat', 'POST', { message: '分析失败任务' });
  assert.equal(conversation.status, 'running');
  assert.equal(conversation.turns[0].content, '分析失败任务');
  const url = `/api/ai/conversations/${conversation.id}`;
  assert.equal((await json('/api/ai/conversations')).length, 1);
  assert.equal((await request('/api/ai/chat', 'POST', { conversation_id: conversation.id, message: '重复请求' })).status, 409);
  await json(url + '/stop', 'POST');
  assert.equal((await json(url)).turns.at(-1).status, 'stopped');
  await json(url, 'PATCH', { title: '新的标题' });
  assert.equal((await json(url)).title, '新的标题');
  assert.equal((await request(url + '/actions/execute', 'POST')).status, 409);
  await json(url, 'DELETE');
  assert.equal((await json('/api/ai/conversations')).length, 0);
});

test('settings and skills can be explored without storing credentials', async () => {
  const { json, request } = demo();
  await json('/api/ai/settings', 'PUT', { enabled: false, model: 'demo-model', api_key: 'DO-NOT-SAVE' });
  const settings = await json('/api/ai/settings');
  assert.equal(settings.model, 'demo-model');
  assert.equal(settings.has_api_key, false);
  assert.deepEqual((await json('/api/ai/models', 'POST', { base_url: 'https://example.test/v1' })).models, ['演示模型（固定示例回复）']);
  assert.ok(!JSON.stringify(settings).includes('DO-NOT-SAVE'));
  assert.equal((await request('/api/ai/chat', 'POST', { message: 'hello' })).status, 400);
  const count = (await json('/api/ai/skills')).length;
  await json('/api/ai/skills', 'PUT', { source: '---\nname: test-skill\ndescription: 示例\n---\n正文' });
  assert.equal((await json('/api/ai/skills')).length, count + 1);
  assert.equal((await json('/api/ai/skills/test-skill')).description, '示例');
  await json('/api/ai/skills/test-skill', 'DELETE');
  assert.equal((await json('/api/ai/skills')).length, count);
  const downloader = await json('/api/downloaders', 'POST', { name: 'demo', password: 'DO-NOT-SAVE' });
  assert.ok(!JSON.stringify(downloader).includes('DO-NOT-SAVE'));
  assert.equal((await request('https://external.example/api/test')).status, 409);
  assert.equal((await request('/api/unknown')).status, 409);
  assert.equal((await request('/api/update/apply', 'POST')).status, 409);
  assert.equal((await request('/api/presets', 'POST', { mode: 'yaml' })).status, 409);
  assert.equal((await demo().json('/api/ai/settings')).enabled, true);
});

test('AI translation follows the shared AI switch and preserves preset translation settings', async () => {
  const { json } = demo();
  await json('/api/ai/settings', 'PUT', { enabled: true, search_enabled: false });
  const runtime = await json('/api/runtime');
  assert.equal(runtime.ai_enabled, true);
  assert.equal(runtime.ai_translation_enabled, undefined);
  const preset = await json('/api/presets', 'POST', { name: 'AI 翻译示例', mode: 'form', form: {
    translator: { ai_enabled: true, engine: { name: 'google' }, fields: { title: true, plot: false } },
  } });
  assert.deepEqual(preset.form_values.translator, { ai_enabled: true, engine: { name: 'google' }, fields: { title: true, plot: false } });
  await json('/api/ai/settings', 'PUT', { enabled: false });
  assert.equal((await json('/api/runtime')).ai_enabled, false);
  const saved = (await json('/api/presets')).find(item => item.id === preset.id);
  assert.equal(saved.form_values.translator.ai_enabled, true);
  assert.equal(saved.form_values.translator.engine.name, 'google');
  assert.ok((await json('/api/ai/skills')).some(item => item.name === 'javsp-translate'));
});

test('preset forms, schedules, path mappings and demo exit', async () => {
  const { json, events, context } = demo();
  const preset = await json('/api/presets', 'POST', { name: '演示预设', mode: 'form', form: { scanner: { input_directory: '/video' } } });
  assert.equal(preset.form_values.scanner.input_directory, '/video');
  const schedule = await json('/api/auto-scrape-schedules', 'POST', { name: '演示规则', cron: '0 2 * * *', enabled: false });
  assert.equal((await json('/api/auto-scrape-schedules')).length, 2);
  await json(`/api/auto-scrape-schedules/${schedule.id}`, 'DELETE');
  assert.equal((await json('/api/auto-scrape-schedules')).length, 1);
  const mappings = [{ source_path: '/downloads', target_path: '/video' }];
  assert.deepEqual((await json('/api/path-mappings', 'PUT', { mappings })).mappings, mappings);
  let stopped = false;
  events.click({ target: { closest: () => true }, preventDefault() {}, stopImmediatePropagation() { stopped = true; } });
  assert.equal(stopped, true);
  assert.equal(context.location.href, 'index.html');
});

test('personal profile changes stay in the offline demo', async () => {
  const { json } = demo();
  assert.equal((await json('/api/profile')).display_name, '演示用户');
  const profile = await json('/api/profile', 'PUT', { display_name: '新名字', avatar_id: 'example' });
  assert.equal(profile.display_name, '新名字');
  assert.equal((await json('/api/profile')).avatar_id, 'example');
});
