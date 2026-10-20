// 媒体库前端的离线回归：标签页切换、库内多文件夹、卡片状态、详情保存与“保存并刮削”。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/library.js'), 'utf8');

function makeElement(value = '') {
  const classes = new Set();
  return {
    value, textContent: '', innerHTML: '', hidden: false, disabled: false, checked: true,
    options: [], dataset: {}, showModal() {}, close() {},
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      toggle: (name, force) => {
        const next = force === undefined ? !classes.has(name) : Boolean(force);
        if (next) classes.add(name); else classes.delete(name);
        return next;
      },
      contains: (name) => classes.has(name),
      includes: (name) => classes.has(name),
    },
    querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}, select() {},
  };
}

function boot({ role = 'admin', routes = {}, runtime = {} } = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const calls = [];
  const listeners = {};
  const storage = new Map();
  const sandboxWindow = { JavspState: { user: { role }, runtime }, setInterval: () => 0, clearInterval: () => {} };
  const localStorage = { getItem: (key) => (storage.has(key) ? storage.get(key) : null), setItem: (key, value) => storage.set(key, String(value)) };
  const context = vm.createContext({
    console,
    window: sandboxWindow,
    setTimeout, clearTimeout, Number, Math, Date, String, URLSearchParams, localStorage,
    document: {
      addEventListener: (name, callback) => { listeners[name] = callback; },
      getElementById: element,
      querySelector: () => null,
    },
    api: async (url, options = {}) => {
      calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
      const key = Object.keys(routes).find((pattern) => (pattern.endsWith('*')
        ? url.startsWith(pattern.slice(0, -1))
        : url.split('?')[0] === pattern));
      if (!key) throw new Error(`未模拟的请求: ${url}`);
      const route = routes[key];
      return typeof route === 'function' ? route(url, options) : route;
    },
    showToast: (message) => { calls.push({ toast: message }); },
    confirmAction: ({ run }) => { calls.push({ confirm: true }); return run(); },
  });
  vm.runInContext(source, context);
  return { context, element, calls, listeners, library: sandboxWindow.JavspLibrary };
}

const folder = (id, name, extra = {}) => ({ id, name, path: `/media/${name}`, recursive: true, film_count: 1, scan_status: 'succeeded', ...extra });
const libraryEntry = (id, name, folders, extra = {}) => ({
  id, name, preset_id: 'default', folders, film_count: folders.reduce((sum, item) => sum + (item.film_count || 0), 0),
  stats: { total: 2, complete: 1, partial: 1, unscraped: 0, failed: 0, missing: 0 }, ...extra,
});

const film = (overrides = {}) => ({
  id: 'f-1', root_id: 'r-1', dvdid: 'ABC-123', title: '演示标题', original_title: '', plot: '',
  release_date: '2024-05-01', studio: '示例片商', publisher: '', director: '', runtime: 120,
  actress: ['演员甲'], size_bytes: 2_000_000_000, files: ['/media/ABC-123/ABC-123.mp4'],
  video_path: '/media/ABC-123/ABC-123.mp4', cover_path: '/media/ABC-123/ABC-123.jpg',
  fanart_count: 5, nfo_path: '/media/ABC-123/movie.nfo', nfo_mtime: 1234, status: 'complete',
  missing_fields: ['publisher'], scrape_state: 'idle', last_task_id: '', last_error: '',
  updated_at: '2026-10-17T10:00:00+08:00', ...overrides,
});

const filmPage = (items, stats = {}) => ({
  items, total: items.length, limit: 48, offset: 0,
  stats: { total: items.length, complete: 0, partial: 0, unscraped: 0, failed: 0, missing: 0, ...stats },
});

async function main() {
  // 1) 卡片：状态徽标、封面地址、无番号时的“填写番号”按钮
  {
    const { library } = boot();
    const complete = library.internals.cardMarkup(film());
    assert.match(complete, /lib-badge" data-tone="success">资料完整/);
    assert.match(complete, /\/api\/library\/films\/f-1\/cover\?v=/);
    assert.match(complete, /data-lib-scrape="f-1">刮削</);

    const unscraped = film({ id: 'f-2', dvdid: '', title: '', cover_path: '', status: 'unscraped', actress: [], runtime: 0, release_date: '', studio: '', missing_fields: ['dvdid', 'title', 'cover'] });
    const unscrapedMarkup = library.internals.cardMarkup(unscraped);
    assert.doesNotMatch(unscrapedMarkup, /\/cover\?v=/);
    assert.match(unscrapedMarkup, /data-tone="neutral">未刮削/);
    assert.match(unscrapedMarkup, /填写番号/);

    assert.match(library.internals.cardMarkup(film({ scrape_state: 'failed', status: 'partial' })), /data-tone="danger">刮削失败/);
    assert.match(library.internals.cardMarkup(film({ status: 'missing' })), /文件已丢失/);
    assert.match(library.internals.cardMarkup(film({ files: ['a.mp4', 'b.mp4', 'c.mp4'] })), /3 个文件/);
    assert.match(library.internals.cardMarkup(film({ detached: 1 })), /已整理/);
    assert.doesNotMatch(library.internals.cardMarkup(film({ detached: 0 })), /已整理/);
  }

  // 2) 统计筛选按钮
  {
    const { library } = boot();
    const markup = library.internals.statsMarkup({ total: 9, complete: 4, partial: 3, unscraped: 2, failed: 1, missing: 0 });
    assert.match(markup, /data-lib-status="all"[^>]*aria-pressed="true"/);
    assert.match(markup, /<strong>4<\/strong><span>资料完整<\/span>/);
    assert.match(markup, /data-lib-status="failed"/);
  }

  // 3) 查询参数：库、状态、搜索都进 URL；“全部”不发送 status
  {
    const { library } = boot();
    const state = library.internals.state;
    state.status = 'all';
    state.query = '';
    state.activeLibraryId = '';
    const params = library.internals.filterParams();
    assert.equal(params.get('status'), null);
    assert.equal(params.get('library_id'), null);
    assert.equal(params.get('limit'), '48');
    state.status = 'partial';
    state.query = '小梅';
    state.activeLibraryId = 'lib-9';
    const filtered = library.internals.filterParams();
    assert.equal(filtered.get('status'), 'partial');
    assert.equal(filtered.get('query'), '小梅');
    assert.equal(filtered.get('library_id'), 'lib-9');
  }

  // 4) 标签页：每个库一个标签，统计数量；管理员多一个“新建”
  {
    const { library, element } = boot();
    const state = library.internals.state;
    state.libraries = [
      libraryEntry('lib-a', '正片', [folder('r-1', 'A', { film_count: 3 })]),
      libraryEntry('lib-b', '素人', [folder('r-2', 'B', { film_count: 5 })]),
    ];
    state.activeLibraryId = 'lib-b';
    const tabs = library.internals.tabsMarkup();
    assert.match(tabs, /data-lib-library="lib-a">正片<span>3<\/span>/);
    assert.match(tabs, /aria-selected="true" data-lib-library="lib-b"/);
    assert.match(tabs, /id="library-create-tab"/);
    // 文件夹条：显示路径名、数量、可移除按钮
    const folders = library.internals.foldersMarkup();
    assert.match(folders, /1 个文件夹/);
    state.libraries[1].minimum_size = 243269632;
    assert.match(library.internals.foldersMarkup(), /忽略小于 232\.0 MB 的文件/);
    assert.match(folders, /lib-folder-name">B</);
    assert.match(folders, /data-lib-remove-folder="r-2"/);

    // 非管理员看不到新建与移除入口
    const operatorView = boot({ role: 'operator' });
    const operatorState = operatorView.library.internals.state;
    operatorState.libraries = state.libraries;
    operatorState.activeLibraryId = 'lib-b';
    assert.doesNotMatch(operatorView.library.internals.tabsMarkup(), /library-create-tab/);
    assert.doesNotMatch(operatorView.library.internals.foldersMarkup(), /data-lib-remove-folder/);
  }

  // 5) load()：先取库列表，再取当前库的影片，并渲染标签页/统计/卡片
  {
    const { library, element, calls } = boot({
      routes: {
        '/api/library/libraries': {
          libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])],
        },
        '/api/library/films': filmPage([film(), film({ id: 'f-2', dvdid: 'DEF-456', status: 'unscraped', title: '', cover_path: '', actress: [], missing_fields: ['title', 'cover'] })], { total: 2, complete: 1, unscraped: 1 }),
      },
    });
    await library.load();
    assert.deepEqual(calls.map((call) => call.url.split('?')[0]), ['/api/library/libraries', '/api/library/films']);
    assert.match(calls[1].url, /library_id=lib-a/);
    assert.match(element('library-tabs').innerHTML, /主库/);
    assert.match(element('library-grid').innerHTML, /ABC-123/);
    assert.match(element('library-stats').innerHTML, /影片总数/);
    assert.equal(element('library-more').hidden, true);
    assert.equal(element('library-scan').disabled, false);
    assert.equal(element('library-body').hidden, false);
    assert.equal(element('library-empty').hidden, true);
  }

  // 6) 没有媒体库时显示空状态，并隐藏主体
  {
    const { library, element } = boot({ routes: { '/api/library/libraries': { libraries: [] } } });
    await library.load();
    assert.equal(element('library-empty').hidden, false);
    assert.equal(element('library-body').hidden, true);
    assert.equal(element('library-scan').disabled, true);
  }

  // 7) 切换标签页：带上新的 library_id 重新加载，并重置筛选
  {
    const { library, listeners, calls } = boot({
      routes: {
        '/api/library/libraries': {
          libraries: [libraryEntry('lib-a', '正片', [folder('r-1', 'A')]), libraryEntry('lib-b', '素人', [folder('r-2', 'B')])],
        },
        '/api/library/films': filmPage([film()]),
      },
    });
    await library.load();
    library.internals.state.status = 'partial';
    library.internals.state.query = '旧搜索';
    listeners.click({ target: { closest: (selector) => (selector === '[data-lib-library]' ? { dataset: { libLibrary: 'lib-b' } } : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 30));
    const last = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.match(last.url, /library_id=lib-b/);
    assert.doesNotMatch(last.url, /status=/);
    assert.doesNotMatch(last.url, /query=/);
    assert.equal(library.internals.state.activeLibraryId, 'lib-b');
  }

  // 8) 新建媒体库：提交名称/预设/首个文件夹，并切到新库
  {
    const { library, element, calls } = boot({
      routes: {
        '/api/library/libraries': (url, options) => (options.method === 'POST'
          ? { library: libraryEntry('lib-new', '新库', [folder('r-9', 'media2')]), scan: { id: 's-1', status: 'running' } }
          : { libraries: [libraryEntry('lib-new', '新库', [folder('r-9', 'media2')])] }),
        '/api/library/scans/s-1': { scan: { id: 's-1', status: 'succeeded', discovered: 1, added: 1, updated: 0, removed: 0 } },
        '/api/library/films': filmPage([film()]),
      },
    });
    element('library-create-name').value = '  新库  ';
    element('library-create-path').value = '  /media2  ';
    element('library-create-preset').value = 'default';
    await library.createLibrary();
    const created = calls.find((call) => call.url === '/api/library/libraries' && call.method === 'POST');
    assert.equal(created.body.name, '新库');
    assert.deepEqual([...created.body.folders], [{ path: '/media2', recursive: true }]);
    assert.equal(created.body.scan, true);
    assert.equal(library.internals.state.activeLibraryId, 'lib-new');
  }

  // 9) 添加文件夹：POST 到当前库的 folders 接口
  {
    const { library, element, calls } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '正片', [folder('r-1', 'A')])] },
        '/api/library/films': filmPage([film()]),
        '/api/library/libraries/lib-a/folders': (url, options) => (options.method === 'POST'
          ? { folder: folder('r-2', 'B'), library: libraryEntry('lib-a', '正片', [folder('r-1', 'A'), folder('r-2', 'B')]), scan: { id: 's-9', status: 'running' } }
          : {}),
        '/api/library/scans/s-9': { scan: { id: 's-9', status: 'succeeded', discovered: 1, added: 1, updated: 0, removed: 0 } },
      },
    });
    await library.load();
    element('library-folder-path').value = '/media/B';
    element('library-folder-name').value = 'B 目录';
    await library.addFolder();
    const added = calls.find((call) => call.url === '/api/library/libraries/lib-a/folders');
    assert.equal(added.method, 'POST');
    assert.equal(added.body.path, '/media/B');
    assert.equal(added.body.name, 'B 目录');
    assert.equal(added.body.scan, true);
  }

  // 10) 移除文件夹：发 DELETE，并提示只删索引
  {
    const { library, element, calls } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '正片', [folder('r-1', 'A'), folder('r-2', 'B')])] },
        '/api/library/films': filmPage([film()]),
        '/api/library/libraries/lib-a/folders/r-2': { ok: true },
      },
    });
    await library.load();
    let confirmed = '';
    element('library-film-dialog'); // 占位，避免其它逻辑取不到元素
    const original = library.internals.state.libraries;
    await library.removeFolder('r-2');
    const removed = calls.find((call) => call.url === '/api/library/libraries/lib-a/folders/r-2');
    assert.ok(removed, '应发送删除请求');
    assert.equal(removed.method, 'DELETE');
    assert.deepEqual(original, library.internals.state.libraries);
  }

  // 11) 详情保存：PATCH 带资料与 NFO mtime；不勾选刮削时不创建任务
  {
    const saved = film({ title: '新标题' });
    const { library, element, calls } = boot({
      routes: {
        '/api/library/films/f-1': { film: saved },
        '/api/library/films/f-1/nfo': { content: '<movie><title>演示标题</title></movie>', path: '/media/ABC-123/movie.nfo', mtime: 1234 },
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])] },
        '/api/library/films': filmPage([]),
      },
    });
    await library.open('f-1');
    const dialog = element('library-film-dialog');
    const fields = new Map();
    ['dvdid', 'title', 'original_title', 'actress', 'release_date', 'studio', 'publisher', 'director', 'runtime', 'plot']
      .forEach((name) => fields.set(name, makeElement()));
    fields.get('dvdid').value = 'ABC-123';
    fields.get('title').value = '新标题';
    fields.get('actress').value = '演员甲、演员乙';
    fields.get('runtime').value = '120';
    dialog.querySelectorAll = () => [...fields.entries()].map(([name, input]) => ({ dataset: { libField: name }, value: input.value }));
    await library.save({ scrape: false });
    const patch = calls.find((call) => call.method === 'PATCH');
    assert.ok(patch, '应发送 PATCH');
    assert.equal(patch.url, '/api/library/films/f-1');
    assert.equal(patch.body.title, '新标题');
    assert.deepEqual([...patch.body.actress], ['演员甲', '演员乙']);
    assert.equal(patch.body.runtime, 120);
    assert.equal(patch.body.write_nfo, true);
    assert.equal(patch.body.expected_nfo_mtime, 1234);
    assert.equal(calls.filter((call) => call.url).some((call) => call.url.includes('/scrape')), false);
  }

  // 12) 保存并刮削：PATCH 之后再 POST /scrape，并把新番号一起提交
  {
    const saved = film({ dvdid: 'NEW-999' });
    const { library, element, calls } = boot({
      routes: {
        '/api/library/films/f-1': { film: saved },
        '/api/library/films/f-1/nfo': { content: '', path: '', mtime: 0 },
        '/api/library/films/f-1/scrape': { task_id: 'task-1', film: saved },
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])] },
        '/api/library/films': filmPage([]),
      },
    });
    await library.open('f-1');
    const dialog = element('library-film-dialog');
    const dvdid = makeElement();
    dvdid.value = 'NEW-999';
    dialog.querySelectorAll = () => [{ dataset: { libField: 'dvdid' }, value: dvdid.value }];
    await library.save({ scrape: true });
    const scrape = calls.find((call) => call.url.endsWith('/scrape'));
    assert.ok(scrape, '应创建刮削任务');
    assert.equal(scrape.method, 'POST');
    assert.equal(scrape.body.dvdid, 'NEW-999');
  }

  // 13) 详情页对非管理员隐藏保存能力（仅浏览与刮削）
  {
    const { library, element } = boot({
      role: 'operator',
      routes: {
        '/api/library/films/f-1': { film: film() },
        '/api/library/films/f-1/nfo': { content: '', path: '', mtime: 0 },
      },
    });
    await library.open('f-1');
    assert.equal(element('library-film-save').disabled, true);
    assert.equal(element('library-film-scrape').disabled, false);
  }

  // 14) 扫描：按媒体库发起，轮询到 succeeded 后刷新
  {
    let polls = 0;
    const { library, element, calls } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])] },
        '/api/library/libraries/lib-a/scan': { scan: { id: 's-1', status: 'running' } },
        '/api/library/scans/s-1': () => ({ scan: { id: 's-1', status: ++polls < 2 ? 'running' : 'succeeded', discovered: 3, added: 1, updated: 1, removed: 1, skipped: 2 } }),
        '/api/library/films': filmPage([film()]),
      },
    });
    await library.load();
    await library.scan();
    assert.match(element('library-message').textContent, /扫描完成：发现 3 部，新增 1/);
    assert.match(element('library-message').textContent, /忽略/);
    assert.ok(calls.some((call) => call.url === '/api/library/libraries/lib-a/scan' && call.method === 'POST'));
  }

  // 15) 翻页与刷新：追加用已加载条数作为 offset；刷新从 0 开始并保留已加载数量
  {
    const pageOf = (offset, limit) => ({
      items: Array.from({ length: Math.min(limit, 60 - offset) }, (_, index) => film({ id: `f-${offset + index}`, dvdid: `ABC-${offset + index}` })),
      total: 60, limit, offset,
      stats: { total: 60, complete: 60, partial: 0, unscraped: 0, failed: 0, missing: 0 },
    });
    const { library, calls } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])] },
        '/api/library/films': (url) => {
          const params = new URL(url, 'https://example.test').searchParams;
          return pageOf(Number(params.get('offset') || 0), Number(params.get('limit') || 48));
        },
      },
    });
    await library.load();
    assert.equal(library.internals.state.films.length, 48);
    await library.refresh();
    const filmsCalls = calls.filter((call) => call.url.startsWith('/api/library/films'));
    assert.equal(filmsCalls.length, 2);
    assert.match(filmsCalls[0].url, /offset=0/);
    assert.match(filmsCalls[1].url, /offset=0/);
    assert.match(filmsCalls[1].url, /limit=48/);
    assert.match(filmsCalls[1].url, /library_id=lib-a/);
  }

  // 16) 分类浏览：选“按女优”会拉取分组，点分组把 group_by/group 带进影片请求
  {
    const { library, element, calls, listeners } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])] },
        '/api/library/groups': { by: 'actress', groups: [{ key: '演员甲', name: '演员甲', count: 2 }, { key: '', name: '未知女优', count: 1 }], total: 3, stats: {} },
        '/api/library/films': filmPage([film()], { total: 3 }),
      },
    });
    await library.load();
    assert.equal(element('library-groups').hidden, true, '不分组时不显示分组条');
    listeners.change({ target: { id: 'library-group', value: 'actress' } });
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.equal(element('library-groups').hidden, false);
    // 默认卡片视图：选分类后主内容是分类卡片，影片墙让位
    assert.match(element('library-groups').innerHTML, /data-lib-group-card="演员甲"/);
    assert.equal(element('library-grid').hidden, true, '分类视图不显示影片墙');
    assert.equal(calls.filter((call) => call.url.startsWith('/api/library/films')).length, 1,
      '分类视图不应再请求影片墙');
    listeners.click({ target: { closest: (selector) => (selector === '[data-lib-group-card]' ? { dataset: { libGroupCard: '演员甲' } } : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 25));
    const last = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.match(last.url, /group_by=actress/);
    assert.match(last.url, /group=%E6%BC%94%E5%91%98%E7%94%B2/);
    assert.match(last.url, /library_id=lib-a/);
  }

  // 17) “打开文件所在目录”：本地部署显示按钮，容器部署隐藏；点击调用 reveal 接口
  {
    const filmData = film();
    const local = boot({
      routes: { '/api/library/films/f-1': { film: filmData }, '/api/library/films/f-1/nfo': { content: '', path: '', mtime: 0 } },
      runtime: { docker: false, local_client: true },
    });
    await local.library.open('f-1');
    const markup = local.element('library-film-content').innerHTML;
    assert.match(markup, /data-lib-reveal="f-1">打开文件所在目录/);

    const container = boot({
      routes: { '/api/library/films/f-1': { film: filmData }, '/api/library/films/f-1/nfo': { content: '', path: '', mtime: 0 } },
      runtime: { docker: true, local_client: true },
    });
    await container.library.open('f-1');
    assert.doesNotMatch(container.element('library-film-content').innerHTML, /data-lib-reveal/);

    // 局域网其它设备：即使不是 Docker 也不能显示（点了会让主机弹窗）
    const remote = boot({
      routes: { '/api/library/films/f-1': { film: filmData }, '/api/library/films/f-1/nfo': { content: '', path: '', mtime: 0 } },
      runtime: { docker: false, local_client: false },
    });
    await remote.library.open('f-1');
    assert.doesNotMatch(remote.element('library-film-content').innerHTML, /data-lib-reveal/,
      '非本机客户端不应出现“打开文件所在目录”');

    local.calls.length = 0;
    const button = makeElement();
    button.dataset = { libReveal: 'f-1' };
    local.listeners.click({ target: { closest: (selector) => (selector === '[data-lib-reveal]' ? button : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const revealCall = local.calls.find((call) => call.url && call.url.endsWith('/reveal'));
    assert.ok(revealCall, '应调用 reveal 接口');
    assert.equal(revealCall.url, '/api/library/films/f-1/reveal');
    assert.equal(revealCall.method, 'POST');
  }

  // 18) 分类卡片模式：切换后显示大卡片，点卡片进入该分组，返回按钮复位
  {
    const { library, element, listeners, calls } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '主库', [folder('r-1', 'A')])] },
        '/api/library/groups': {
          by: 'actress',
          groups: [
            { key: '演员甲', name: '演员甲', count: 2, covers: [{ id: 'f-1', v: 'x' }, { id: 'f-2', v: 'y' }] },
            { key: '演员乙', name: '演员乙', count: 1, covers: [] },
            { key: '', name: '未知女优', count: 1, covers: [] },
          ],
          total: 4, all: { count: 4, covers: [{ id: 'f-1', v: 'x' }] }, stats: {},
        },
        '/api/library/films': filmPage([film()], { total: 2 }),
      },
    });
    await library.load();
    const state = library.internals.state;
    state.groupBy = 'actress';
    state.group = null;
    await library.refresh();

    // 默认卡片模式：名字渲染成封面大卡
    assert.equal(state.groupMode, 'cards');
    assert.match(element('library-groups').innerHTML, /class="lc-library-card" data-lib-group-card="演员甲"/);

    // 切到标签模式
    listeners.click({ target: { closest: (selector) => (selector === '#library-group-mode' ? {} : null) }, preventDefault() {} });
    assert.equal(state.groupMode, 'chips');
    assert.match(element('library-groups').innerHTML, /lib-group[^"]*"[^>]*data-lib-group="演员甲"/);
    assert.doesNotMatch(element('library-groups').innerHTML, /lc-library-card/);

    // 再切回卡片模式
    listeners.click({ target: { closest: (selector) => (selector === '#library-group-mode' ? {} : null) }, preventDefault() {} });
    assert.equal(state.groupMode, 'cards');
    const cards = element('library-groups').innerHTML;
    // 与概览一致的卡片结构：lc-library-grid + lc-library-card + 16:9 封面模板
    assert.match(cards, /class="lc-library-grid"/);
    assert.match(cards, /class="lc-library-card" data-lib-group-card="演员甲"/);
    assert.match(cards, /lc-library-meta"><strong[^>]*>演员甲<\/strong><small><i><\/i>2 部影片/);
    assert.match(cards, /\/api\/library\/films\/f-1\/cover\?v=x/);
    assert.match(cards, /class="lc-cover lc-tpl-/);
    assert.match(cards, /按女优浏览/);
    assert.doesNotMatch(cards, /data-lib-group-card="__all__"/, '分类卡片里不出现“全部影片”');
    assert.match(element('library-tabs').innerHTML, /data-lib-library="__all__">全部影片<\/span>|<span>/, '顶部标签含全部影片');
    assert.match(element('library-tabs').innerHTML, /data-lib-library="__all__">全部影片<span>/);
    assert.equal(element('library-groups').classList.includes('lib-groups--cards'), true, '卡片模式容器切换为块级');
    assert.equal(element('library-groups').hidden, false);
    assert.equal(element('library-grid').hidden, true, '卡片模式下不显示影片墙');
    assert.equal(element('library-more').hidden, true);
    assert.equal(element('library-group-mode').hidden, false);
    assert.equal(element('library-group-mode').textContent, '列表视图');

    // 搜索框仍然可用：搜索会带着 query 重新取分组，并且不会清掉已选分组
    state.query = '演员甲';
    await library.refresh();
    const groupCall = calls.filter((call) => call.url.startsWith('/api/library/groups')).at(-1);
    assert.match(groupCall.url, /query=%E6%BC%94%E5%91%98%E7%94%B2/);
    state.query = '';

    // 点卡片进入该分组
    listeners.click({ target: { closest: (selector) => (selector === '[data-lib-group-card]' ? { dataset: { libGroupCard: '演员甲' } } : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(state.group, '演员甲');
    const filmCall = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.match(filmCall.url, /group_by=actress/);
    assert.match(filmCall.url, /group=%E6%BC%94%E5%91%98%E7%94%B2/);
    assert.equal(element('library-grid').hidden, false, '进入分组后显示影片');
    assert.match(element('library-group-nav').innerHTML, /返回女优列表/);
    assert.equal(element('library-group-nav').hidden, false);
    assert.equal(element('library-groups').hidden, true, '进入分类后不再显示其它名字');

    // 排序仍然作用于影片列表
    listeners.change({ target: { id: 'library-sort', value: 'release' } });
    await new Promise((resolve) => setTimeout(resolve, 30));
    const sorted = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.match(sorted.url, /sort=release/);
    assert.match(sorted.url, /group=%E6%BC%94%E5%91%98%E7%94%B2/);

    // 返回分组列表
    listeners.click({ target: { closest: (selector) => (selector === '#library-group-back' ? {} : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(state.group, null);
    assert.equal(element('library-group-nav').hidden, true);
    assert.equal(element('library-groups').hidden, false, '返回后重新显示分类');
    assert.match(element('library-groups').innerHTML, /lc-library-grid/, '返回后仍保持卡片模式');

    assert.equal(state.group, null);

    // 未知分组（空 key）也能点进去
    listeners.click({ target: { closest: (selector) => (selector === '[data-lib-group-card]' ? { dataset: { libGroupCard: '' } } : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(state.group, '');
    const unknownCall = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.match(unknownCall.url, /group_by=actress/);
    assert.match(unknownCall.url, /[?&]group=&/);
  }

  // 19) 顶部“全部影片”标签：跨库汇总，不带 library_id，隐藏库级操作
  {
    const { library, element, listeners, calls } = boot({
      routes: {
        '/api/library/libraries': {
          libraries: [
            libraryEntry('lib-a', '骑兵', [folder('r-1', 'A', { film_count: 3 })]),
            libraryEntry('lib-b', '步兵', [folder('r-2', 'B', { film_count: 5 })]),
          ],
        },
        '/api/library/films': filmPage([film()], { total: 8 }),
        '/api/library/libraries/lib-a/scan': { scan: { id: 's-a', status: 'running' } },
        '/api/library/libraries/lib-b/scan': { scan: { id: 's-b', status: 'running' } },
        '/api/library/scans/s-a': { scan: { id: 's-a', status: 'succeeded', discovered: 3, added: 0, updated: 0, removed: 0 } },
        '/api/library/scans/s-b': { scan: { id: 's-b', status: 'succeeded', discovered: 5, added: 0, updated: 0, removed: 0 } },
      },
    });
    await library.load();
    const state = library.internals.state;
    assert.equal(state.activeLibraryId, 'lib-a', '默认选第一个库');
    assert.match(element('library-tabs').innerHTML, /data-lib-library="__all__">全部影片<span>8<\/span>/);

    listeners.click({ target: { closest: (selector) => (selector === '[data-lib-library]' ? { dataset: { libLibrary: '__all__' } } : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(state.activeLibraryId, '__all__');
    const filmsCall = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.doesNotMatch(filmsCall.url, /library_id=/, '全部影片不按库过滤');
    assert.match(element('library-folders').innerHTML, /全部媒体库 · 2 个库/);
    assert.equal(element('library-add-folder').hidden, true, '全部影片下不显示添加文件夹');
    assert.equal(element('library-settings').hidden, true);

    // 扫描会依次扫描每个库
    await library.scan();
    assert.ok(calls.some((call) => call.url === '/api/library/libraries/lib-a/scan'));
    assert.ok(calls.some((call) => call.url === '/api/library/libraries/lib-b/scan'));

    // 回到具体库
    listeners.click({ target: { closest: (selector) => (selector === '[data-lib-library]' ? { dataset: { libLibrary: 'lib-b' } } : null) }, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(state.activeLibraryId, 'lib-b');
    const backCall = calls.filter((call) => call.url.startsWith('/api/library/films')).at(-1);
    assert.match(backCall.url, /library_id=lib-b/);
    assert.equal(element('library-add-folder').hidden, false);
  }

  // 20) 批量刮削：按当前筛选提交，并提示预设必需字段
  {
    const { library, element, calls, listeners } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '骑兵', [folder('r-1', 'A', { film_count: 3 })])] },
        '/api/library/films': filmPage([film()], { total: 3, partial: 3 }),
        '/api/presets': [{ id: 'default', name: '默认配置', form: { crawler: { required_keys: ['dvdid', 'cover', 'title'] } } }],
        '/api/library/films/scrape': { accepted: ['t1', 't2'], skipped: [], total: 2 },
      },
    });
    await library.load();
    const state = library.internals.state;
    state.status = 'partial';
    state.query = 'ABC';
    await library.refresh();
    state.status = 'partial';
    state.query = 'ABC';
    element('library-batch-scrape').disabled = false;
    await library.batchScrape();
    await new Promise((resolve) => setTimeout(resolve, 30));  // confirmAction 的回调是异步执行的
    const call = calls.find((item) => item.url === '/api/library/films/scrape');
    assert.ok(call, '应调用批量刮削接口');
    assert.equal(call.method, 'POST');
    assert.equal(call.body.library_id, 'lib-a');
    assert.equal(call.body.status, 'partial');
    assert.equal(call.body.query, 'ABC');
    assert.equal(call.body.preset_id, 'default');
    assert.ok(calls.some((item) => item.toast && /已加入 2 个/.test(item.toast)), '应提示已加入数量');
    assert.equal(element('library-batch-scrape').disabled, false);
  }

  // 21) 没有影片时批量刮削直接提示，不发请求
  {
    const { library, calls } = boot({
      routes: {
        '/api/library/libraries': { libraries: [libraryEntry('lib-a', '骑兵', [folder('r-1', 'A')])] },
        '/api/library/films': filmPage([], { total: 0 }),
      },
    });
    await library.load();
    await library.batchScrape();
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(calls.filter((item) => item.url === '/api/library/films/scrape').length, 0);
    assert.ok(calls.some((item) => item.toast && /没有可刮削/.test(item.toast)));
  }

  // 22) 手动刮削页：原生选择框只在本机显示
  {
    const app = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/app.js'), 'utf8');
    assert.match(app, /nativeHidden = Boolean\(runtime\.docker\) \|\| runtime\.local_client === false/,
      '原生路径按钮要同时判断 docker 与 local_client');
    assert.match(app, /nativePickerAllowed = state\.runtime\?\.local_client !== false/,
      '预设里的“选择路径”按钮也要按本机判断');
  }

  // 23) 会动宿主机文件的操作只对主机上的页面开放
  {
    const app = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/app.js'), 'utf8');
    assert.match(app, /function hostFileActionsAllowed\(\)/, '需要本机判断助手');
    assert.match(app, /task\.restore_available && hostFileActionsAllowed\(\)/,
      '还原文件按钮要按本机判断');
  }

  // 24) 媒体库播放：详情里有播放入口，进度存本地
  {
    const librarySource = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/library.js'), 'utf8');
    assert.match(librarySource, /data-lib-play="\$\{escape\(film\.id\)\}"/, '详情里要有播放按钮');
    assert.match(librarySource, /javsp-web\.playback\./, '播放进度要存在 localStorage');
    assert.match(librarySource, /\/api\/library\/films\/\$\{encodeURIComponent\(filmId\)\}\/playback/,
      '播放要走 playback 接口');
    const page = fs.readFileSync(path.join(__dirname, '../javsp_web/web/index.html'), 'utf8');
    assert.match(page, /id="library-player-video"/, '播放器元素要存在');
    assert.match(page, /id="library-player-box"/, 'ArtPlayer 容器要存在');
    assert.match(page, /\/assets\/artplayer\.min\.js/, '要加载 ArtPlayer');
    assert.match(librarySource, /new window\.ArtPlayer\(/, '有 ArtPlayer 时用它渲染');
    assert.match(page, /id="library-player-dialog"/, '播放器是独立大窗');
    assert.match(librarySource, /class="lib-cover-play"/, '卡片封面要有悬停播放按钮');
    assert.match(librarySource, /function openPlayer\(/, '播放走 openPlayer');
    const css = fs.readFileSync(path.join(__dirname, '../javsp_web/web/assets/overrides.css'), 'utf8');
    assert.match(css, /\.lib-cover-wrap:hover \.lib-cover-play/, '悬停才显示播放按钮');
    assert.match(css, /width: min\(1920px, 96vw\)/, '播放窗口按 1920 上限自适应');
    assert.match(librarySource, /function abortMediaElement\(/, '关闭时要真正断开媒体连接');
    assert.match(librarySource, /playback\/stop/, '关闭时要通知服务端结束播放');
    assert.match(librarySource, /keepalive: true/, '关闭页面时也要发出停止请求');
    assert.doesNotMatch(librarySource, /payload\.mode !== 'emby'\) return;/, '本地播放关闭时也要停流');
    assert.match(librarySource, /function watchPlaybackStall\(/, '要有卡住检测');
    assert.match(librarySource, /readyState >= 2/, '卡住判断看是否拿到元数据');
    assert.match(librarySource, /已改用转码播放/, '卡住后要自动改走转码');
    assert.match(page, /\/assets\/hls\.min\.js/, '转码用 HLS 播放，需要 hls.js');
    assert.match(page, /id="library-player-mode"/, '播放器里要有直连/转码切换按钮');
    assert.match(librarySource, /customType: \{/, 'ArtPlayer 要注册 m3u8 自定义类型');
    assert.match(librarySource, /function switchPlaybackMode\(/, '要有模式切换逻辑');
    assert.match(librarySource, /prefer: prefer|query\.set\('prefer'/, '切换模式要带 prefer 参数');
    assert.match(librarySource, /query\.set\('start'/, '切换模式要保持播放位置');
    assert.match(librarySource, /destroyPlayer\(\{ keepDialog: true \}\)/, '切换模式时不要关播放窗口');
    assert.match(librarySource, /state\.playerSwitching = true/, '切换期间要屏蔽 close 事件');
    assert.match(librarySource, /function abortAllMedia\(/, '关闭时要清理所有残留媒体元素');
    assert.ok(fs.existsSync(path.join(__dirname, '../javsp_web/web/assets/hls.min.js')), 'hls.js 要随仓库分发');
    assert.match(librarySource, /fallback_mode === 'emby'/, '本地播不了要回退到 Emby');
    assert.ok(fs.existsSync(path.join(__dirname, '../javsp_web/web/assets/artplayer.min.js')), 'ArtPlayer 文件要随仓库分发');
  }

  console.log('library client tests passed');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
