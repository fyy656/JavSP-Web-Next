/**
 * 媒体库页面：顶部是媒体库标签页，每个媒体库可以包含多个文件夹。
 *
 * 数据源是磁盘上的目录索引（/api/library/*），任务记录只是历史。所有写操作都由
 * 服务端再校验权限；删除媒体库或文件夹只删索引，不动磁盘文件。
 */
(() => {
  const LIMIT = 48;
  // 分类卡片一次渲染多少个：637 个女优时全量 DOM 会明显卡顿
  const GROUP_PAGE = 60;
  // 顶部标签里虚拟的“全部影片”（跨所有媒体库汇总）
  const ALL_LIBRARY = '__all__';
  const STATUS = {
    complete: { label: '资料完整', tone: 'success' },
    partial: { label: '缺资料', tone: 'warning' },
    unscraped: { label: '未刮削', tone: 'neutral' },
    missing: { label: '文件已丢失', tone: 'danger' },
    conflict: { label: '资料有冲突', tone: 'warning' },
  };
  const REQUIRED_LABELS = {
    dvdid: '番号', cid: 'CID', url: 'URL', cover: '封面', big_cover: '高清封面', title: '标题',
    plot: '剧情', genre: '类型', score: '评分', ori_title: '原始标题', magnet: '下载链接',
    serial: '系列', actress: '演员', director: '导演', duration: '时长', producer: '制作商',
    publisher: '发行商', publish_date: '发行日期', preview_pics: '剧照', preview_video: '预告片',
  };
  const MISSING_LABELS = {
    dvdid: '番号', title: '标题', cover: '封面', actress: '女优',
    studio: '片商', release_date: '发行日期',
  };

  const GROUP_MODE_KEY = 'javsp-web.library-group-mode';
  const readGroupMode = () => {
    try {
      // 默认卡片视图：选了分类直接看封面大卡，比一整面标签更直观
      return localStorage.getItem(GROUP_MODE_KEY) === 'chips' ? 'chips' : 'cards';
    } catch (_) {
      return 'cards';
    }
  };
  const state = {
    libraries: [], activeLibraryId: '', films: [], total: 0, stats: {}, offset: 0,
    status: 'all', query: '', sort: 'updated', groupBy: '', group: null, groupMode: readGroupMode(), groups: [],
    groupsVisible: GROUP_PAGE, librariesFresh: false, playingFilmId: '', previousFilmId: '', playerSwitching: false,
    allCount: 0, allCovers: [],
    loading: false, loaded: false, active: null, nfo: null, pollTimer: null, busy: false,
  };

  const appState = () => (typeof window !== 'undefined' ? window.JavspState || null : null);
  const isAdmin = () => appState()?.user?.role === 'admin';
  // 容器部署运行在另一台机器上，无法打开浏览器所在电脑的文件夹
  // 只有跑在服务端主机上的浏览器才显示“打开文件所在目录”：
  // 局域网其它设备点击会让服务端主机弹出文件管理器，必须隐藏（服务端也会 403）。
  const canReveal = () => {
    const runtime = appState()?.runtime;
    return Boolean(runtime) && !runtime.docker && runtime.local_client === true;
  };

  function el(id) {
    return document.getElementById(id);
  }

  function message(text) {
    const target = el('library-message');
    if (target) target.textContent = text || '';
  }

  function escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[character]));
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    const pad = (value) => String(value).padStart(2, '0');
    return hours ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
  }

  function formatBytes(value) {
    const number = Number(value) || 0;
    if (!number) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const unit = Math.min(Math.floor(Math.log(number) / Math.log(1024)), units.length - 1);
    return `${(number / (1024 ** unit)).toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }

  const activeLibrary = () => state.libraries.find((item) => item.id === state.activeLibraryId) || null;
  const folders = () => activeLibrary()?.folders || [];

  function folderLabel(folder) {
    const name = String(folder.name || '').trim();
    if (name) return name;
    return String(folder.path || '').split(/[\\/]/).filter(Boolean).pop() || folder.path;
  }

  function toneFor(film) {
    if (film.scrape_state === 'queued' || film.scrape_state === 'running') return { label: '刮削中', tone: 'info' };
    if (film.scrape_state === 'failed' && film.status !== 'complete') return { label: '刮削失败', tone: 'danger' };
    return STATUS[film.status] || STATUS.unscraped;
  }

  function filterParams(extra = {}) {
    const params = new URLSearchParams();
    if (state.activeLibraryId && !libraryIsAll()) params.set('library_id', state.activeLibraryId);
    if (state.status && state.status !== 'all') params.set('status', state.status);
    if (state.query) params.set('query', state.query);
    if (state.groupBy && state.group !== null && state.group !== '__all__') {
      // 空字符串代表“未知女优/未知片商”，不能省略，否则会变成“不分组”
      params.set('group_by', state.groupBy);
      params.set('group', state.group);
    }
    params.set('sort', state.sort);
    params.set('limit', String(extra.limit ?? LIMIT));
    params.set('offset', String(extra.offset ?? 0));
    return params;
  }

  // ------------------------------------------------------------------ 渲染
  function libraryIsAll() {
    return state.activeLibraryId === ALL_LIBRARY;
  }

  function allLibraryCount() {
    return state.libraries.reduce((sum, library) => sum + (library.film_count || 0), 0);
  }

  function tabsMarkup() {
    const tabs = [];
    if (state.libraries.length) {
      const active = libraryIsAll();
      tabs.push(`<button class="lib-tab${active ? ' active' : ''}" type="button" role="tab" aria-selected="${active}" `
        + `data-lib-library="${ALL_LIBRARY}">全部影片<span>${allLibraryCount()}</span></button>`);
    }
    state.libraries.map((library) => {
      const active = library.id === state.activeLibraryId;
      tabs.push(`<button class="lib-tab${active ? ' active' : ''}" type="button" role="tab" aria-selected="${active}" `
        + `data-lib-library="${escape(library.id)}">${escape(library.name)}<span>${library.film_count || 0}</span></button>`);
    });
    if (isAdmin()) {
      tabs.push('<button class="lib-tab lib-tab-add" type="button" id="library-create-tab">＋ 新建媒体库</button>');
    }
    return tabs.join('');
  }

  function foldersMarkup() {
    if (libraryIsAll()) {
      const total = allLibraryCount();
      return `<span class="lib-folders-label">全部媒体库 · ${state.libraries.length} 个库 · ${total} 部影片</span>`;
    }
    const library = activeLibrary();
    if (!library) return '';
    const chips = library.folders.map((folder) => `<span class="lib-folder" title="${escape(folder.path)}">`
      + `<span class="lib-folder-name">${escape(folderLabel(folder))}</span>`
      + `<span class="lib-folder-count">${folder.film_count || 0}</span>`
      + (isAdmin() ? `<button class="lib-folder-remove" type="button" data-lib-remove-folder="${escape(folder.id)}" aria-label="从媒体库移除">×</button>` : '')
      + '</span>');
    const missing = library.folders.filter((folder) => folder.scan_status === 'failed').length;
    const threshold = Number(library.minimum_size || 0);
    const summary = library.folders.length
      ? `${library.folders.length} 个文件夹${missing ? `，其中 ${missing} 个扫描失败` : ''}`
        + (threshold > 0 ? ` · 忽略小于 ${formatBytes(threshold)} 的文件` : '')
      : '还没有文件夹';
    return `<span class="lib-folders-label">${escape(summary)}</span>${chips.join('')}`;
  }

  function statsMarkup(stats) {
    const items = [
      ['all', '影片总数', stats.total || 0],
      ['complete', '资料完整', stats.complete || 0],
      ['partial', '缺资料', stats.partial || 0],
      ['unscraped', '未刮削', stats.unscraped || 0],
      ['failed', '刮削失败', stats.failed || 0],
      ['missing', '文件丢失', stats.missing || 0],
    ];
    return items.map(([key, label, count]) => {
      const active = (key === 'all' && state.status === 'all') || key === state.status;
      return `<button class="lib-stat${active ? ' active' : ''}" type="button" data-lib-status="${key}" aria-pressed="${active}">`
        + `<strong>${count}</strong><span>${label}</span></button>`;
    }).join('');
  }

  function groupLabel(group) {
    return group.name || group.key || '未知';
  }

  function groupsMarkup() {
    if (!state.groupBy) return '';
    const visibleChips = state.groups.slice(0, Math.max(GROUP_PAGE, state.groupsVisible || GROUP_PAGE));
    const items = visibleChips.map((group) => {
      const active = state.group !== null && group.key === state.group;
      return `<button class="lib-group${active ? ' active' : ''}" type="button" data-lib-group="${escape(group.key)}" aria-pressed="${active}">`
        + `${escape(groupLabel(group))}<span>${group.count}</span></button>`;
    });
    const allActive = state.group === null;
    const remaining = state.groups.length - visibleChips.length;
    const more = remaining > 0
      ? `<button class="lib-group lib-group-more" type="button" id="library-groups-more">显示更多（${remaining}）</button>`
      : '';
    return `<button class="lib-group${allActive ? ' active' : ''}" type="button" data-lib-group="__all__" aria-pressed="${allActive}">全部<span>${state.total}</span></button>`
      + items.join('') + more;
  }

  // 与概览页一致的 16:9 封面拼贴模板（lc-* 类来自 cinema.css），只是封面走媒体库接口
  function groupSeed(text) {
    let hash = 2166136261;
    for (const char of String(text)) {
      hash ^= char.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function groupCoverMarkup(group, english) {
    const covers = (group.covers || []).map((cover) => `/api/library/films/${encodeURIComponent(cover.id)}/cover?v=${encodeURIComponent(cover.v || '')}`);
    const seed = groupSeed(`${english}:${group.key}`);
    const pickSeed = Math.imul(seed ^ (seed >>> 15), 2246822507) >>> 13;
    const template = covers.length >= 5
      ? ['tilted', 'upright', 'fan', 'strip'][pickSeed % 4]
      : covers.length ? ['stacked', 'diagonal', 'fan'][pickSeed % 3] : 'plain';
    const pick = (count) => Array.from({ length: count }, (_, index) => (covers.length ? covers[index % covers.length] : ''));
    const image = (src) => (src ? `<img src="${src}" alt="" loading="lazy" decoding="async">` : '<i class="lc-blank"></i>');
    const posters = (list, extra = '') => list
      .map((src, index) => `<span class="lc-poster${extra}" style="--i:${index}">${image(src)}</span>`).join('');
    const backdrop = covers.length ? `<span class="lc-backdrop">${image(covers[0])}</span>` : '';
    const [first, second, third, fourth, fifth] = pick(5);
    const layers = {
      tilted: `${backdrop}<span class="lc-tilt">${posters(pick(9))}</span>`,
      upright: `<span class="lc-wall">${posters(pick(12))}</span>`,
      fan: `<span class="lc-fan">${posters([fourth, second, first, third, fifth])}</span>`,
      strip: `${backdrop}<span class="lc-strip">${posters(pick(5))}</span>`,
      stacked: `<span class="lc-stack">${posters([first, first])}<span class="lc-poster lc-main">${image(first)}</span></span>`,
      diagonal: `<span class="lc-diagonal">${image(first)}</span>`,
      plain: '',
    }[template];
    const name = groupLabel(group);
    const length = [...String(name)].length;
    const scale = length <= 2 ? 1 : length <= 4 ? .82 : length <= 6 ? .64 : length <= 9 ? .5 : .38;
    return `<span class="lc-cover lc-tpl-${template}" style="--hue:${seed % 360};--title-scale:${scale}">`
      + `${layers}<span class="lc-cover-title"><b>${escape(name)}</b><em>${escape(english)}</em></span></span>`;
  }

  const GROUP_ENGLISH = { actress: 'ACTRESS', studio: 'STUDIO', year: 'YEAR' };

  function groupCardsMarkup() {
    if (!state.groupBy) return '';
    if (!state.groups.length) {
      const label = { actress: '女优', studio: '片商', year: '年份' }[state.groupBy] || '分类';
      return `<div class="library-empty lc-empty-state"><h3>还没有可分类的${escape(label)}</h3><p>扫描出影片后，这里会按${escape(label)}生成封面卡片。</p></div>`;
    }
    const english = GROUP_ENGLISH[state.groupBy] || 'GROUP';
    const label = { actress: '女优', studio: '片商', year: '年份' }[state.groupBy] || '分类';
    const visible = state.groups.slice(0, Math.max(GROUP_PAGE, state.groupsVisible || GROUP_PAGE));
    const cards = visible.map((group) => {
      const name = groupLabel(group);
      return `<button type="button" class="lc-library-card" data-lib-group-card="${escape(group.key)}" aria-label="查看 ${escape(name)} 的影片">`
        + groupCoverMarkup(group, english)
        + `<span class="lc-library-meta"><strong title="${escape(name)}">${escape(name)}</strong><small><i></i>${group.count} 部影片</small></span>`
        + '</button>';
    });
    const remaining = state.groups.length - visible.length;
    const more = remaining > 0
      ? `<div class="lc-groups-more"><button class="button secondary" type="button" id="library-groups-more">显示更多分类（剩余 ${remaining}）</button></div>`
      : '';
    return `<div class="lc-section-head"><h3>按${escape(label)}浏览</h3><span>${state.groups.length} 个分类 · ${state.total} 部影片</span></div>`
      + `<div class="lc-library-grid">${cards.join('')}</div>` + more;
  }

  function groupNavMarkup() {
    if (!state.groupBy || state.group === null) return '';
    const label = { actress: '女优', studio: '片商', year: '年份' }[state.groupBy] || '分类';
    const current = state.groups.find((group) => group.key === state.group);
    const name = current ? groupLabel(current) : (state.group || '未知');
    return `<button class="button secondary small" type="button" id="library-group-back">← 返回${label}列表</button>`
      + `<span class="muted">当前：${escape(name)}</span>`;
  }

  function cardMarkup(film) {
    const status = toneFor(film);
    const cover = film.cover_path
      ? `<img loading="lazy" alt="" src="/api/library/films/${encodeURIComponent(film.id)}/cover?v=${encodeURIComponent(film.updated_at || '')}" onerror="this.closest('.lib-cover').classList.add('lib-cover--empty');this.remove()">`
      : '';
    const placeholder = `<span class="lib-cover-text">${escape(film.dvdid || (film.files?.[0] || '').split(/[\\/]/).pop() || '无封面')}</span>`;
    const title = film.title || '（缺少标题）';
    const meta = [film.detached ? '已整理' : '', film.release_date?.slice(0, 4), film.studio].filter(Boolean).join(' · ') || '—';
    const parts = (film.files || []).length > 1 ? `<span class="lib-parts">${film.files.length} 个文件</span>` : '';
    const action = film.scrape_state === 'queued' || film.scrape_state === 'running'
      ? '<button class="button small" type="button" disabled>刮削中</button>'
      : film.dvdid
        ? `<button class="button small secondary" type="button" data-lib-scrape="${escape(film.id)}">刮削</button>`
        : `<button class="button small secondary" type="button" data-lib-scrape="${escape(film.id)}">填写番号</button>`;
    return `<article class="lib-card" data-lib-film="${escape(film.id)}">
      <div class="lib-cover-wrap">
      <button class="lib-cover${cover ? '' : ' lib-cover--empty'}" type="button" data-lib-open="${escape(film.id)}" aria-label="打开 ${escape(film.dvdid || '影片')} 详情">${cover}${placeholder}<span class="lib-badge" data-tone="${status.tone}">${status.label}</span>${parts}</button>
      <button class="lib-cover-play" type="button" data-lib-play="${escape(film.id)}" aria-label="播放 ${escape(film.dvdid || '影片')}"><span aria-hidden="true">▶</span></button>
      </div>
      <div class="lib-info">
        <strong class="lib-id">${escape(film.dvdid || '—')}</strong>
        <span class="lib-title" title="${escape(film.title)}">${escape(title)}</span>
        <small class="lib-meta">${escape(meta)}</small>
      </div>
      <div class="lib-actions">${action}<button class="button small" type="button" data-lib-open="${escape(film.id)}" title="编辑番号等资料">资料</button></div>
    </article>`;
  }

  function gridMarkup() {
    if (!state.films.length) {
      if (!state.libraries.length) return '';
      if (libraryIsAll()) {
        return `<p class="lib-empty">${state.query || state.status !== 'all' ? '没有符合条件的影片。' : '所有媒体库都还没有影片。'}</p>`;
      }
      const library = activeLibrary();
      if (!library || !library.folders.length) return '<p class="lib-empty">这个媒体库还没有文件夹。点右上角“添加文件夹”把存放影片的目录加进来。</p>';
      return `<p class="lib-empty">${state.query || state.status !== 'all' ? '没有符合条件的影片。' : '这个媒体库的文件夹里还没有影片。点“扫描”重新读取目录。'}</p>`;
    }
    return state.films.map(cardMarkup).join('');
  }

  function render() {
    const tabs = el('library-tabs');
    if (tabs) tabs.innerHTML = tabsMarkup();
    const empty = el('library-empty');
    const body = el('library-body');
    const hasLibraries = state.libraries.length > 0;
    if (empty) empty.hidden = hasLibraries;
    if (body) body.hidden = !hasLibraries;
    const foldersTarget = el('library-folders');
    if (foldersTarget) foldersTarget.innerHTML = foldersMarkup();
    const stats = el('library-stats');
    if (stats) stats.innerHTML = statsMarkup(state.stats || {});
    const groups = el('library-groups');
    const drilled = state.group !== null;
    const showCards = state.groupBy && !drilled && state.groupMode === 'cards';
    if (groups) {
      groups.innerHTML = showCards ? groupCardsMarkup() : groupsMarkup();
      groups.classList.toggle('lib-groups--cards', showCards);
      // 点进某个分类后不再显示其它名字，只留下返回条和影片墙
      groups.hidden = !state.groupBy || drilled;
    }
    const groupToolbar = el('library-group-toolbar');
    if (groupToolbar) groupToolbar.hidden = !state.groupBy;
    const nav = el('library-group-nav');
    if (nav) {
      nav.innerHTML = groupNavMarkup();
      nav.hidden = !state.groupBy || !drilled;
    }
    const modeButton = el('library-group-mode');
    if (modeButton) {
      modeButton.hidden = !state.groupBy;
      modeButton.textContent = state.groupMode === 'cards' ? '列表视图' : '卡片视图';
      modeButton.title = state.groupMode === 'cards' ? '切换为标签列表' : '切换为封面卡片';
    }
    const grid = el('library-grid');
    if (grid) {
      grid.innerHTML = showCards ? '' : gridMarkup();
      grid.hidden = showCards;
    }
    const more = el('library-more');
    if (more) {
      const remaining = state.total - state.films.length;
      more.hidden = showCards || remaining <= 0;
      more.textContent = remaining > 0 ? `加载更多（还有 ${remaining} 部）` : '加载更多';
    }
    const scanButton = el('library-scan');
    if (scanButton) scanButton.disabled = !hasLibraries;
    const batchButton = el('library-batch-scrape');
    if (batchButton) {
      batchButton.disabled = !hasLibraries || !state.activeLibraryId || !state.total;
      batchButton.title = `把当前筛选的 ${state.total} 部影片加入刮削队列`;
    }
    const addFolder = el('library-add-folder');
    if (addFolder) addFolder.hidden = !isAdmin() || !hasLibraries || libraryIsAll();
    const settings = el('library-settings');
    if (settings) settings.hidden = !isAdmin() || !hasLibraries || libraryIsAll();
    renderPolling();
  }

  // ------------------------------------------------------------------ 数据
  async function loadLibraries() {
    const payload = await api('/api/library/libraries');
    state.libraries = payload.libraries || [];
    if (state.activeLibraryId !== ALL_LIBRARY
        && state.activeLibraryId && !state.libraries.some((item) => item.id === state.activeLibraryId)) {
      state.activeLibraryId = '';
    }
    if (!state.activeLibraryId && state.libraries.length) state.activeLibraryId = state.libraries[0].id;
    return state.libraries;
  }

  async function loadGroups() {
    if (!state.groupBy) {
      state.groups = [];
      state.allCovers = [];
      state.allCount = 0;
      state.group = null;
      return;
    }
    const params = new URLSearchParams({ by: state.groupBy });
    if (state.activeLibraryId && !libraryIsAll()) params.set('library_id', state.activeLibraryId);
    if (state.query) params.set('query', state.query);
    const payload = await api(`/api/library/groups?${params}`);
    state.groups = payload.groups || [];
    state.allCount = Number(payload.all?.count ?? 0);
    state.allCovers = payload.all?.covers || [];
    state.groupsVisible = GROUP_PAGE;
    // 分类视图不请求影片墙，统计和总数直接用分组接口的结果
    if (payload.stats) state.stats = payload.stats;
    if (payload.all?.count !== undefined) state.total = Number(payload.all.count);
    // 搜索会过滤分组列表，此时分组暂时缺席是正常的，不要清掉已选分组
    const known = state.group === '__all__' || state.groups.some((item) => item.key === state.group);
    if (state.group !== null && !state.query && !known) state.group = null;
  }

  async function loadFilms({ append = false, limit = LIMIT } = {}) {
    if (!state.activeLibraryId) {
      state.films = [];
      state.total = 0;
      state.stats = {};
      state.offset = 0;
      return;
    }
    // 处于“全部影片”时照常请求，只是不带 library_id，由服务端汇总所有库
    const offset = append ? state.films.length : 0;
    const params = filterParams({ offset, limit });
    const payload = await api(`/api/library/films?${params}`);
    const items = payload.items || [];
    state.total = payload.total || 0;
    state.stats = payload.stats || {};
    state.films = append ? [...state.films, ...items] : items;
    state.offset = state.films.length;
  }

  function browsingGroups() {
    // 选了分类但还没点进具体某一项：主内容是分类，不需要影片墙
    return Boolean(state.groupBy) && state.group === null;
  }

  async function load({ keepGroup = false, refreshLibraries = true } = {}) {
    if (state.loading) return;
    state.loading = true;
    message('正在读取媒体库…');
    try {
      if (refreshLibraries) await loadLibraries();
      if (!keepGroup) state.group = null;
      await loadGroups();
      if (!browsingGroups()) await loadFilms();
      else state.films = [];
      state.loaded = true;
      message('');
      render();
    } catch (error) {
      message(error.message || '读取媒体库失败');
    } finally {
      state.loading = false;
    }
  }

  async function refresh() {
    try {
      await loadLibraries();
      await loadGroups();
      if (browsingGroups()) {
        state.films = [];
      } else {
        await loadFilms({ limit: Math.min(200, Math.max(LIMIT, state.films.length)) });
      }
      render();
    } catch (error) {
      message(error.message);
    }
  }

  async function selectLibrary(libraryId) {
    if (!libraryId || libraryId === state.activeLibraryId) return;
    state.activeLibraryId = libraryId;
    state.status = 'all';
    state.query = '';
    state.group = null;
    state.offset = 0;
    const search = el('library-page-search');
    if (search) search.value = '';
    message('');
    await load({ keepGroup: true, refreshLibraries: false });
  }

  // ------------------------------------------------------------------ 扫描
  async function pollScan(scanId) {
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      const { scan } = await api(`/api/library/scans/${encodeURIComponent(scanId)}`);
      if (scan.status === 'succeeded') {
        const skipped = Number(scan.skipped || 0);
        message(`扫描完成：发现 ${scan.discovered} 部，新增 ${scan.added}，更新 ${scan.updated}，移除 ${scan.removed}（源文件已删除或不再符合扫描条件）`
          + (skipped ? `；另有 ${skipped} 个文件小于预设的最小匹配文件大小，已忽略` : ''));
        return scan;
      }
      if (scan.status === 'failed') throw new Error(scan.error || '扫描失败');
      message(`正在扫描…已发现 ${scan.discovered || 0} 部`);
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
    throw new Error('扫描时间过长，请稍后在媒体库中刷新查看');
  }

  async function scan() {
    if (!state.activeLibraryId) return;
    const targets = libraryIsAll() ? state.libraries.map((library) => library.id) : [state.activeLibraryId];
    if (!targets.length) return;
    const button = el('library-scan');
    if (button) button.disabled = true;
    try {
      for (const libraryId of targets) {
        const { scan: job } = await api(`/api/library/libraries/${encodeURIComponent(libraryId)}/scan`,
          { method: 'POST' });
        await pollScan(job.id);
      }
      await refresh();
    } catch (error) {
      message(error.message || '扫描失败');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function presetRequiredKeys(presetId) {
    try {
      const presets = await api('/api/presets');
      const preset = (presets || []).find((item) => item.id === presetId);
      const keys = (((preset || {}).form || {}).crawler || {}).required_keys || [];
      return keys.map((key) => REQUIRED_LABELS[key] || key);
    } catch (error) {
      return [];
    }
  }

  function scopeDescription() {
    const library = activeLibrary();
    const parts = [libraryIsAll() ? '全部影片' : (library ? library.name : '当前媒体库')];
    if (state.status && state.status !== 'all') {
      const label = { complete: '资料完整', partial: '缺资料', unscraped: '未刮削', failed: '刮削失败', missing: '文件丢失' }[state.status];
      parts.push(label ? `只看「${label}」` : state.status);
    }
    if (state.query) parts.push(`搜索“${state.query}”`);
    if (state.groupBy && state.group !== null) {
      const current = state.groups.find((group) => group.key === state.group);
      const name = current ? groupLabel(current) : (state.group || '未知');
      parts.push(`分类：${name}`);
    }
    return parts.join(' · ');
  }

  async function batchScrape() {
    if (!state.activeLibraryId) return;
    if (!state.total) {
      showToast('当前筛选没有可刮削的影片', 'danger');
      return;
    }
    const library = activeLibrary();
    const presetId = library ? library.preset_id : '';
    const required = await presetRequiredKeys(presetId);
    const requiredText = required.length
      ? `当前预设必须取得：${required.join('、')}；抓不到这些字段的任务会失败。`
      : '当前预设没有设置“必须取得的资料”。';
    confirmAction({
      title: '批量刮削',
      text: `将把「${scopeDescription()}」共 ${state.total} 部影片加入刮削队列。`
        + '已标记“资料完整”的影片同样会入队，用于补齐 NFO 里缺的字段（例如女优）；'
        + '引擎会重写 NFO，但程序会把原有字段补回，不会丢资料。'
        + requiredText,
      confirmLabel: '开始刮削',
      run: async () => {
        const payload = {
          library_id: libraryIsAll() ? '' : state.activeLibraryId,
          status: state.status === 'all' ? '' : state.status,
          query: state.query,
          group_by: state.groupBy && state.group !== null ? state.groupBy : '',
          group: state.groupBy && state.group !== null ? state.group : '',
          preset_id: presetId || undefined,
        };
        try {
          const result = await api('/api/library/films/scrape', { method: 'POST', body: JSON.stringify(payload) });
          const accepted = (result.accepted || []).length;
          const skipped = (result.skipped || []).length;
          showToast(skipped ? `已加入 ${accepted} 个任务，跳过 ${skipped} 个` : `已加入 ${accepted} 个刮削任务`);
          await refresh();
        } catch (error) {
          showToast(error.message || '批量刮削失败', 'danger');
        }
      },
    });
  }

  // ------------------------------------------------------------------ 媒体库与文件夹
  async function fillPresets(select) {
    if (!select) return;
    try {
      const presets = await api('/api/presets');
      select.innerHTML = (presets || []).map((preset) => `<option value="${escape(preset.id)}">${escape(preset.name || preset.id)}</option>`).join('');
    } catch (error) {
      select.innerHTML = '<option value="default">默认预设</option>';
    }
  }

  function openCreateDialog() {
    const dialog = el('library-create-dialog');
    if (!dialog) return;
    el('library-create-name').value = '';
    el('library-create-path').value = '';
    el('library-create-message').textContent = '';
    fillPresets(el('library-create-preset'));
    dialog.showModal();
  }

  async function createLibrary() {
    const name = String(el('library-create-name')?.value || '').trim();
    const path = String(el('library-create-path')?.value || '').trim();
    const preset = el('library-create-preset')?.value || 'default';
    const recursive = el('library-create-recursive')?.checked !== false;
    const target = el('library-create-message');
    if (!name) {
      if (target) target.textContent = '请填写媒体库名称';
      return;
    }
    if (target) target.textContent = '正在创建…';
    try {
      const payload = await api('/api/library/libraries', {
        method: 'POST',
        body: JSON.stringify({
          name,
          preset_id: preset,
          folders: path ? [{ path, recursive }] : [],
          scan: Boolean(path),
        }),
      });
      el('library-create-dialog')?.close();
      state.activeLibraryId = payload.library.id;
      state.status = 'all';
      state.query = '';
      state.group = null;
      state.offset = 0;
      if (payload.scan?.id) await pollScan(payload.scan.id);
      await load({ keepGroup: false });
      showToast('媒体库已创建');
    } catch (error) {
      if (target) target.textContent = error.message || '创建失败';
    }
  }

  function openFolderDialog() {
    const library = activeLibrary();
    if (!library) return;
    el('library-folder-path').value = '';
    el('library-folder-name').value = '';
    el('library-folder-message').textContent = '';
    el('library-folder-context').textContent = `添加到媒体库：${library.name}`;
    el('library-folder-dialog')?.showModal();
  }

  async function addFolder() {
    const library = activeLibrary();
    if (!library) return;
    const path = String(el('library-folder-path')?.value || '').trim();
    const name = String(el('library-folder-name')?.value || '').trim();
    const recursive = el('library-folder-recursive')?.checked !== false;
    const target = el('library-folder-message');
    if (!path) {
      if (target) target.textContent = '请填写文件夹路径';
      return;
    }
    if (target) target.textContent = '正在添加并扫描…';
    try {
      const payload = await api(`/api/library/libraries/${encodeURIComponent(library.id)}/folders`, {
        method: 'POST',
        body: JSON.stringify({ path, name, recursive, scan: true }),
      });
      el('library-folder-dialog')?.close();
      if (payload.scan?.id) await pollScan(payload.scan.id);
      await refresh();
      showToast('文件夹已添加');
    } catch (error) {
      if (target) target.textContent = error.message || '添加失败';
    }
  }

  async function removeFolder(folderId) {
    const library = activeLibrary();
    if (!library) return;
    const folder = folders().find((item) => item.id === folderId);
    if (!folder) return;
    confirmAction({
      title: '移除文件夹',
      text: `确定把“${folderLabel(folder)}”从媒体库“${library.name}”移除吗？只删除索引，磁盘上的视频、NFO 与图片不会删除。`,
      confirmLabel: '移除',
      danger: true,
      run: async () => {
        try {
          await api(`/api/library/libraries/${encodeURIComponent(library.id)}/folders/${encodeURIComponent(folderId)}`,
            { method: 'DELETE' });
          await refresh();
          showToast('文件夹已移除');
        } catch (error) {
          message(error.message || '移除失败');
        }
      },
    });
  }

  function openSettings() {
    const library = activeLibrary();
    if (!library) return;
    el('library-settings-name').value = library.name;
    el('library-settings-message').textContent = '';
    fillPresets(el('library-settings-preset'));
    const select = el('library-settings-preset');
    if (select) select.value = library.preset_id;
    el('library-settings-dialog')?.showModal();
  }

  async function saveSettings() {
    const library = activeLibrary();
    if (!library) return;
    const name = String(el('library-settings-name')?.value || '').trim();
    const preset = el('library-settings-preset')?.value || 'default';
    const target = el('library-settings-message');
    try {
      await api(`/api/library/libraries/${encodeURIComponent(library.id)}`,
        { method: 'PUT', body: JSON.stringify({ name, preset_id: preset }) });
      el('library-settings-dialog')?.close();
      await refresh();
      showToast('设置已保存');
    } catch (error) {
      if (target) target.textContent = error.message || '保存失败';
    }
  }

  function deleteLibrary() {
    const library = activeLibrary();
    if (!library) return;
    confirmAction({
      title: '删除媒体库',
      text: `确定删除媒体库“${library.name}”吗？只会移除索引，磁盘上的视频、NFO 与图片不会删除。`,
      confirmLabel: '删除',
      danger: true,
      run: async () => {
        try {
          await api(`/api/library/libraries/${encodeURIComponent(library.id)}`, { method: 'DELETE' });
          el('library-settings-dialog')?.close();
          state.activeLibraryId = '';
          await load();
          showToast('媒体库已删除');
        } catch (error) {
          const target = el('library-settings-message');
          if (target) target.textContent = error.message || '删除失败';
        }
      },
    });
  }

  // ------------------------------------------------------------------ 播放（本地直连优先，Emby 兜底）
  const PLAYBACK_KEY_PREFIX = 'javsp-web.playback.';
  const playbackKey = (filmId) => PLAYBACK_KEY_PREFIX + filmId;
  let player = null;          // ArtPlayer 实例（没有 ArtPlayer 时退回原生 <video>）
  let playerTimer = 0;
  let stallTimer = 0;
  let playerPayload = null;

  function clearStallTimer() {
    if (typeof window.clearTimeout === 'function') window.clearTimeout(stallTimer);
    stallTimer = 0;
  }

  function watchPlaybackStall() {
    clearStallTimer();
    if (typeof window.setTimeout !== 'function') return;
    stallTimer = window.setTimeout(async () => {
      if (!state.playingFilmId) return;
      const video = playerVideo();
      if (video && video.readyState >= 2) return;      // 已经拿到元数据，说明在播
      if (playerPayload?.fallback_mode !== 'emby' || playbackRetrying) return;
      playbackRetrying = true;
      const ok = await retryWithEmby();
      playbackRetrying = false;
      if (ok) {
        showToast('直连播放没有开始（可能是编码不被浏览器支持），已改用转码播放', 'danger');
      }
    }, 6000);
  }

  function readPlaybackPosition(filmId) {
    try {
      const value = Number(localStorage.getItem(playbackKey(filmId)));
      return Number.isFinite(value) && value > 0 ? value : 0;
    } catch (error) {
      return 0;
    }
  }

  function savePlaybackPosition(filmId, seconds) {
    try {
      if (!filmId || !Number.isFinite(seconds) || seconds < 1) return;
      localStorage.setItem(playbackKey(filmId), String(Math.floor(seconds)));
    } catch (error) {
      /* 隐私模式下忽略 */
    }
  }

  function clearPlaybackPosition(filmId) {
    try {
      localStorage.removeItem(playbackKey(filmId));
    } catch (error) {
      /* ignore */
    }
  }

  function playbackElements() {
    return {
      panel: el('library-player-dialog'),
      box: el('library-player-box'),
      video: el('library-player-video'),
      info: el('library-player-info'),
    };
  }

  function playerVideo() {
    return player?.video || playbackElements().video || null;
  }

  function setPlayerSource(url) {
    if (player) {
      player.url = url;
      return;
    }
    const { video } = playbackElements();
    if (!video) return;
    video.src = url;
  }

  function abortMediaElement(element) {
    if (!element || typeof element.pause !== 'function') return;
    try {
      element.pause();
      // 必须清空 src 再 load()，浏览器才会真正断开连接，否则服务端会一直读盘
      element.removeAttribute('src');
      element.load();
    } catch (error) {
      /* ignore */
    }
  }

  function abortAllMedia() {
    abortMediaElement(playerVideo());
    const { video, box } = playbackElements();
    abortMediaElement(video);
    const sweep = (node) => {
      if (!node || typeof node.querySelectorAll !== 'function') return;
      node.querySelectorAll('video,audio').forEach((element) => abortMediaElement(element));
    };
    sweep(box);
    sweep(el('library-player-dialog'));
    sweep(document.body);
  }

  function notifyPlaybackStopped(payload) {
    const filmId = state.playingFilmId;
    if (!filmId) return;
    const body = JSON.stringify({ server_id: payload.server_id || '', play_session_id: payload.play_session_id || '' });
    try {
      // 用 keepalive 保证关闭页面/对话框时也能把请求发出去
      fetch(`/api/library/films/${encodeURIComponent(filmId)}/playback/stop`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true,
      }).catch(() => {});
    } catch (error) {
      /* ignore */
    }
  }

  function destroyPlayer({ keepDialog = false } = {}) {
    const { video, info } = playbackElements();
    const current = playerVideo();
    if (state.playingFilmId && current && Number.isFinite(current.currentTime)) {
      savePlaybackPosition(state.playingFilmId, current.currentTime);
    }
    notifyPlaybackStopped(playerPayload);
    if (player) {
      try { player.pause(); } catch (error) { /* ignore */ }
      try { if (typeof player.destroy === 'function') player.destroy(false); } catch (error) { /* ignore */ }
      player = null;
    }
    // ArtPlayer 销毁后 <video> 可能还挂在 DOM 上继续播（只剩声音），这里统一清干净
    abortAllMedia();
    const { box } = playbackElements();
    if (box) box.innerHTML = '';
    if (info) info.textContent = '';
    clearStallTimer();
    if (!keepDialog) {
      const dialog = el('library-player-dialog');
      if (dialog && dialog.open) {
        try { dialog.close(); } catch (error) {
          try { dialog.removeAttribute('open'); } catch (inner) { /* ignore */ }
        }
      }
    }
    state.playingFilmId = '';
    playerPayload = null;
    window.clearInterval(playerTimer);
  }

  function ensureArtPlayer() {
    const { box, video } = playbackElements();
    if (!box || typeof window.ArtPlayer !== 'function') return null;
    if (video) video.hidden = true;
    box.hidden = false;
    if (player) return player;
    try {
      player = new window.ArtPlayer({
        container: box,
        url: '',
        autoplay: false,
        customType: {
          m3u8(video, url, art) {
            const Hls = window.Hls;
            if (Hls && typeof Hls.isSupported === 'function' && Hls.isSupported()) {
              if (art.hls) art.hls.destroy();
              const hls = new Hls({ enableWorker: true, lowLatencyMode: false });
              hls.loadSource(url);
              hls.attachMedia(video);
              art.hls = hls;
              art.once('destroy', () => hls.destroy());
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
              art.url = url;
            } else {
              art.notice.show = '当前浏览器不支持 HLS 播放';
            }
          },
        },
        airplay: true,
        theme: '#2f6fed',
        volume: 0.85,
        pip: true,
        fullscreen: true,
        fullscreenWeb: true,
        playbackRate: true,
        aspectRatio: true,
        setting: true,
        hotkey: true,
        miniProgressBar: true,
        autoPlayback: true,
        lang: 'zh-cn',
        moreVideoAttr: { playsInline: true, preload: 'metadata' },
      });
      player.on('video:timeupdate', () => {
        if (!state.playingFilmId || !player) return;
        savePlaybackPosition(state.playingFilmId, player.currentTime);
      });
      player.on('video:ended', () => {
        if (state.playingFilmId) clearPlaybackPosition(state.playingFilmId);
      });
      player.on('video:error', () => {
        handlePlaybackError('ArtPlayer');
      });
      return player;
    } catch (error) {
      player = null;
      // 创建失败时退回原生 <video>，别让用户看到空白播放器
      if (video) video.hidden = false;
      if (box) { box.hidden = true; box.innerHTML = ''; }
      return null;
    }
  }

  async function retryWithEmby() {
    if (!state.playingFilmId) return false;
    try {
      const payload = await api(`/api/library/films/${encodeURIComponent(state.playingFilmId)}/playback?prefer=emby`);
      if (!payload?.url) return false;
      playerPayload = { ...payload, fallback_mode: '' };
      setPlayerSource(payload.url);
      player?.play?.();
      if (player) {
        if (payload.transcoding) showToast('已改用媒体服务器转码播放');
      } else {
        await playerVideo()?.play?.().catch(() => {});
      }
      return true;
    } catch (error) {
      return false;
    }
  }

  let playbackRetrying = false;

  async function handlePlaybackError() {
    const current = playerVideo();
    const fallback = playerPayload?.fallback_url;
    if (fallback && !current?.dataset?.fallbackUsed) {
      if (current?.dataset) current.dataset.fallbackUsed = '1';
      setPlayerSource(fallback);
      player?.play?.();
      showToast('直连放不出来，已改用兜底地址重试', 'danger');
      return;
    }
    if (!playbackRetrying && playerPayload?.fallback_mode === 'emby') {
      playbackRetrying = true;
      const ok = await retryWithEmby();
      playbackRetrying = false;
      if (ok) return;
    }
    const code = current?.error ? current.error.code : '';
    const reason = { 1: '播放被中止', 2: '网络中断', 3: '解码失败', 4: '格式/地址不可用（浏览器不支持该编码）' }[code] || '未知原因';
    showToast(`播放失败：${reason}${code ? `（code ${code}）` : ''}`, 'danger');
  }

  function bindNativePlayer() {
    const { video } = playbackElements();
    if (!video || video.dataset.bound === '1') return;
    video.dataset.bound = '1';
    video.addEventListener('error', handlePlaybackError);
  }

  function openPlayer(filmId) {
    destroyPlayer();
    const dialog = el('library-player-dialog');
    if (dialog && !dialog.open) {
      try { dialog.showModal(); } catch (error) { dialog.setAttribute('open', ''); }
    }
    return playFilm(filmId);
  }

  async function playFilm(filmId, options = {}) {
    const { panel, info } = playbackElements();
    if (!panel) return;
    const prefer = options.mode || 'auto';
    const startAt = Math.max(0, Math.floor(Number(options.start) || 0));
    try {
      const query = new URLSearchParams();
      if (prefer !== 'auto') query.set('prefer', prefer);
      if (startAt > 0) query.set('start', String(startAt));
      const suffix = query.toString() ? `?${query}` : '';
      const payload = await api(`/api/library/films/${encodeURIComponent(filmId)}/playback${suffix}`);
      if (!payload?.url) throw new Error('没有拿到可播放的地址');
      state.playingFilmId = filmId;
      state.previousFilmId = filmId;
      playerPayload = payload;
      updatePlayerModeButton(payload);
      if (info) {
        const source = payload.mode === 'local' ? '本地直连' : (payload.transcoding ? (payload.hls ? '转码播放（HLS）' : '转码播放') : '直连播放');
        info.textContent = `${source}${payload.server_name ? ' · ' + payload.server_name : ''}`;
      }
      const art = ensureArtPlayer();
      if (!art) bindNativePlayer();
      const video = playerVideo();
      if (video?.dataset) video.dataset.fallbackUsed = '';
      setPlayerSource(payload.url);
      const resume = payload.start_seconds > 0 ? payload.start_seconds : readPlaybackPosition(filmId);
      if (resume > 5) {
        const applyResume = () => {
          const current = playerVideo();
          if (!current) return;
          const duration = Number(current.duration);
          if (Number.isFinite(duration) && resume < duration - 10) {
            current.currentTime = resume;
            showToast(`已回到上次位置 ${formatDuration(resume)}`);
          }
        };
        if (art) art.once('ready', applyResume);
        else video?.addEventListener('loadedmetadata', applyResume, { once: true });
      }
      if (art) {
        art.on('video:loadedmetadata', clearStallTimer);
        art.on('video:playing', clearStallTimer);
        art.play().catch(() => {});
        watchPlaybackStall();
      } else {
        video?.addEventListener('loadedmetadata', clearStallTimer, { once: true });
        video?.addEventListener('playing', clearStallTimer, { once: true });
        watchPlaybackStall();
        await video?.play?.().catch((error) => {
          if (error?.name === 'NotAllowedError') {
            showToast('浏览器阻止了自动播放，请再点一次播放键');
            return;
          }
          showToast(`无法开始播放：${error?.name || 'unknown'}`, 'danger');
        });
      }
      window.clearInterval(playerTimer);
      playerTimer = window.setInterval(() => {
        const current = playerVideo();
        if (state.playingFilmId && current && current.currentTime > 0) {
          savePlaybackPosition(state.playingFilmId, current.currentTime);
        }
      }, 5000);
    } catch (error) {
      destroyPlayer();
      showToast(error.message || '暂时无法播放该影片', 'danger');
    }
  }

  function updatePlayerModeButton(payload) {
    const button = el('library-player-mode');
    if (!button) return;
    const isLocal = payload?.mode === 'local';
    button.textContent = isLocal ? '转码' : '直连';
    button.title = isLocal ? '改用媒体服务器转码播放' : '改用本地直连播放';
    button.dataset.targetMode = isLocal ? 'emby' : 'local';
  }

  async function switchPlaybackMode() {
    const button = el('library-player-mode');
    const filmId = state.previousFilmId || state.playingFilmId;
    if (!button || !filmId) return;
    const target = button.dataset.targetMode === 'emby' ? 'emby' : 'local';
    const current = playerVideo();
    const position = current && Number.isFinite(current.currentTime) ? Math.floor(current.currentTime) : 0;
    state.playerSwitching = true;
    try {
      destroyPlayer({ keepDialog: true });
      await playFilm(filmId, { mode: target, start: position });
    } finally {
      state.playerSwitching = false;
    }
  }

  function bindPlayerLifecycle() {
    const playerDialog = el('library-player-dialog');
    if (playerDialog && typeof playerDialog.addEventListener === 'function' && playerDialog.dataset.playerBound !== '1') {
      playerDialog.dataset.playerBound = '1';
      playerDialog.addEventListener('close', () => {
        if (state.playerSwitching) return;
        destroyPlayer();
      });
      playerDialog.addEventListener('cancel', () => {
        if (state.playerSwitching) return;
        destroyPlayer();
      });
      // 点窗口外的遮罩关闭
      playerDialog.addEventListener('click', (event) => {
        if (event.target === playerDialog) destroyPlayer();
      });
      // 关页面/刷新时也通知服务端收尾
      window.addEventListener('pagehide', () => {
        if (state.playingFilmId) notifyPlaybackStopped(playerPayload);
      });
    }
    const dialog = el('library-film-dialog');
    if (!dialog || typeof dialog.addEventListener !== 'function' || dialog.dataset.playerBound === '1') return;
    dialog.dataset.playerBound = '1';
    dialog.addEventListener('close', () => destroyPlayer());
    dialog.addEventListener('cancel', () => destroyPlayer());
  }

  // ------------------------------------------------------------------ 影片详情
  function detailMarkup(film) {
    const status = toneFor(film);
    const missing = (film.missing_fields || []).map((key) => MISSING_LABELS[key] || key);
    const source = { nfo: '来自 NFO', filename: '来自文件名', manual: '手动指定' }[film.dvdid_source] || '未知';
    const cover = film.cover_path
      ? `<img alt="" src="/api/library/films/${encodeURIComponent(film.id)}/cover?v=${encodeURIComponent(film.updated_at || '')}">`
      : '<span class="lib-cover-text">无封面</span>';
    const field = (name, label, value, extra = '') => `<label>${label}<input data-lib-field="${name}" value="${escape(value ?? '')}"${extra}></label>`;
    return `<div class="lib-detail-head">
        <div class="lib-detail-cover${film.cover_path ? '' : ' lib-cover--empty'}">${cover}</div>
        <div class="lib-detail-summary">
          <span class="lib-badge" data-tone="${status.tone}">${status.label}</span>
          ${missing.length ? `<p class="muted">缺少：${escape(missing.join('、'))}</p>` : '<p class="muted">必需资料齐全。</p>'}
          ${film.detached ? '<p class="muted">已整理到整理目录（索引已跟随，不需要重新扫描）</p>' : ''}
          <p class="muted">文件：${escape(film.video_path || '')}</p>
          ${canReveal() ? `<button class="button secondary small" type="button" data-lib-reveal="${escape(film.id)}">打开文件所在目录</button>` : ''}
          <button class="button primary small" type="button" data-lib-play="${escape(film.id)}" title="通过已连接的 Emby / Jellyfin 播放">播放</button>
          <p class="muted">${formatBytes(film.size_bytes)} · ${(film.files || []).length} 个文件${film.fanart_count ? ` · ${film.fanart_count} 张剧照` : ''}</p>
          ${film.last_error ? `<p class="form-error">${escape(film.last_error)}</p>` : ''}
          ${film.last_task_id ? `<p class="muted">最近任务：<code>${escape(film.last_task_id)}</code></p>` : ''}
        </div>
      </div>
      <div class="lib-fields">
        ${field('dvdid', '番号', film.dvdid)}
        <small class="muted">${source}</small>
        ${field('title', '标题', film.title)}
        ${field('original_title', '原始标题', film.original_title)}
        ${field('actress', '女优（用顿号或逗号分隔）', (film.actress || []).join('、'))}
        ${field('release_date', '发行日期', film.release_date, ' placeholder="YYYY-MM-DD"')}
        ${field('studio', '片商', film.studio)}
        ${field('publisher', '发行商', film.publisher)}
        ${field('director', '导演', film.director)}
        ${field('runtime', '时长（分钟）', film.runtime || '', ' type="number" min="0" max="640000"')}
        <label class="lib-plot">剧情简介<textarea data-lib-field="plot" rows="4">${escape(film.plot)}</textarea></label>
      </div>
      <details class="lib-nfo">
        <summary>原始 NFO</summary>
        <p class="muted">高级编辑：直接修改 XML。保存前会校验根节点是否为 &lt;movie&gt;，并保留一份 .bak 备份。</p>
        <textarea data-lib-nfo rows="10" spellcheck="false"></textarea>
        <div class="lib-nfo-actions"><button class="button secondary" type="button" data-lib-nfo-reload>重新读取</button><button class="button secondary" type="button" data-lib-nfo-save>保存原始 NFO</button><span class="muted" data-lib-nfo-state></span></div>
      </details>`;
  }

  async function openFilm(filmId) {
    destroyPlayer();
    try {
      const [{ film }, nfo] = await Promise.all([
        api(`/api/library/films/${encodeURIComponent(filmId)}`),
        api(`/api/library/films/${encodeURIComponent(filmId)}/nfo`).catch(() => ({ content: '' })),
      ]);
      state.active = film;
      state.nfo = nfo || { content: '', mtime: 0 };
      const dialog = el('library-film-dialog');
      const title = el('library-film-title');
      if (title) title.textContent = film.dvdid || '影片详情';
      const content = el('library-film-content');
      if (content) content.innerHTML = detailMarkup(film);
      if (dialog) {
        if (dialog.open) dialog.close();
        dialog.showModal();
      }
      const textarea = dialog?.querySelector('[data-lib-nfo]');
      if (textarea) textarea.value = state.nfo.content || '';
      const save = el('library-film-save');
      if (save) save.disabled = !isAdmin() || state.busy;
      const scrape = el('library-film-scrape');
      if (scrape) scrape.disabled = state.busy;
      const error = el('library-film-message');
      if (error) error.textContent = '';
    } catch (error) {
      showToast(error.message || '读取影片详情失败', 'danger');
    }
  }

  function collectFields() {
    const dialog = el('library-film-dialog');
    const values = {};
    dialog?.querySelectorAll('[data-lib-field]').forEach((input) => {
      values[input.dataset.libField] = input.value;
    });
    values.actress = String(values.actress || '').split(/[、,，;；]/).map((name) => name.trim()).filter(Boolean);
    values.runtime = Number(values.runtime) || 0;
    return values;
  }

  async function saveFilm({ scrape = false } = {}) {
    if (!state.active || state.busy) return;
    const values = collectFields();
    const filmId = state.active.id;
    state.busy = true;
    const messageTarget = el('library-film-message');
    if (messageTarget) messageTarget.textContent = '正在保存…';
    try {
      const payload = {
        ...values,
        write_nfo: true,
        expected_nfo_mtime: Number(state.active.nfo_mtime) || null,
      };
      const saved = await api(`/api/library/films/${encodeURIComponent(filmId)}`,
        { method: 'PATCH', body: JSON.stringify(payload) });
      state.active = saved.film;
      if (scrape) {
        await api(`/api/library/films/${encodeURIComponent(filmId)}/scrape`, {
          method: 'POST',
          body: JSON.stringify({ dvdid: values.dvdid || null }),
        });
        showToast('已保存并开始刮削');
      } else {
        showToast('资料已保存');
      }
      el('library-film-dialog')?.close();
      await refresh();
    } catch (error) {
      if (messageTarget) messageTarget.textContent = error.message || '保存失败';
    } finally {
      state.busy = false;
    }
  }

  async function scrapeFilm(filmId) {
    const film = state.films.find((item) => item.id === filmId);
    if (!film) return;
    if (!film.dvdid) {
      await openFilm(filmId);
      const input = el('library-film-dialog')?.querySelector('[data-lib-field="dvdid"]');
      input?.focus();
      input?.select();
      return;
    }
    try {
      await api(`/api/library/films/${encodeURIComponent(filmId)}/scrape`, { method: 'POST', body: JSON.stringify({}) });
      showToast(`已开始刮削 ${film.dvdid}`);
      await refresh();
    } catch (error) {
      showToast(error.message || '无法开始刮削', 'danger');
    }
  }

  // ------------------------------------------------------------------ 轮询
  function renderPolling() {
    const scraping = state.films.some((film) => film.scrape_state === 'queued' || film.scrape_state === 'running');
    if (scraping && !state.pollTimer) {
      state.pollTimer = window.setInterval(() => { refresh().catch(() => {}); }, 4000);
    } else if (!scraping && state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // ------------------------------------------------------------------ 事件
  document.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-lib-library]');
    if (tab) {
      event.preventDefault();
      selectLibrary(tab.dataset.libLibrary);
      return;
    }
    if (event.target.closest('#library-create-tab') || event.target.closest('#library-empty-create')) {
      event.preventDefault();
      openCreateDialog();
      return;
    }
    if (event.target.closest('#library-create-confirm')) {
      event.preventDefault();
      createLibrary();
      return;
    }
    if (event.target.closest('#library-add-folder')) {
      event.preventDefault();
      openFolderDialog();
      return;
    }
    if (event.target.closest('#library-folder-confirm')) {
      event.preventDefault();
      addFolder();
      return;
    }
    const removeFolderButton = event.target.closest('[data-lib-remove-folder]');
    if (removeFolderButton) {
      event.preventDefault();
      removeFolder(removeFolderButton.dataset.libRemoveFolder);
      return;
    }
    if (event.target.closest('#library-settings')) {
      event.preventDefault();
      openSettings();
      return;
    }
    if (event.target.closest('#library-settings-save')) {
      event.preventDefault();
      saveSettings();
      return;
    }
    if (event.target.closest('#library-settings-delete')) {
      event.preventDefault();
      deleteLibrary();
      return;
    }
    if (event.target.closest('#library-groups-more')) {
      event.preventDefault();
      state.groupsVisible = (state.groupsVisible || GROUP_PAGE) + GROUP_PAGE;
      render();
      return;
    }
    if (event.target.closest('#library-batch-scrape')) {
      event.preventDefault();
      batchScrape();
      return;
    }
    if (event.target.closest('[data-lib-play]')) {
      event.preventDefault();
      event.stopPropagation();
      openPlayer(event.target.closest('[data-lib-play]').dataset.libPlay);
      return;
    }
    if (event.target.closest('#library-player-mode')) {
      event.preventDefault();
      switchPlaybackMode();
      return;
    }
    if (event.target.closest('#library-player-close')) {
      event.preventDefault();
      destroyPlayer();
      return;
    }
    if (event.target.closest('#library-scan')) {
      scan();
      return;
    }
    const stat = event.target.closest('[data-lib-status]');
    if (stat) {
      state.status = stat.dataset.libStatus;
      state.offset = 0;
      refresh();
      return;
    }
    const group = event.target.closest('[data-lib-group]');
    if (group) {
      event.preventDefault();
      state.group = group.dataset.libGroup === '__all__' ? null : group.dataset.libGroup;
      state.offset = 0;
      refresh();
      return;
    }
    const groupCard = event.target.closest('[data-lib-group-card]');
    if (groupCard) {
      event.preventDefault();
      state.group = groupCard.dataset.libGroupCard;
      state.offset = 0;
      refresh();
      return;
    }
    if (event.target.closest('#library-group-back')) {
      event.preventDefault();
      state.group = null;
      state.offset = 0;
      refresh();
      return;
    }
    if (event.target.closest('#library-group-mode')) {
      event.preventDefault();
      state.groupMode = state.groupMode === 'cards' ? 'chips' : 'cards';
      try { localStorage.setItem(GROUP_MODE_KEY, state.groupMode); } catch (_) {}
      render();
      return;
    }
    const open = event.target.closest('[data-lib-open]');
    if (open) {
      event.preventDefault();
      openFilm(open.dataset.libOpen);
      return;
    }
    const scrape = event.target.closest('[data-lib-scrape]');
    if (scrape) {
      event.preventDefault();
      scrapeFilm(scrape.dataset.libScrape);
      return;
    }
    if (event.target.closest('#library-more')) {
      loadFilms({ append: true }).then(render).catch((error) => message(error.message));
      return;
    }
    if (event.target.closest('[data-lib-nfo-reload]')) {
      if (!state.active) return;
      api(`/api/library/films/${encodeURIComponent(state.active.id)}/nfo`).then((nfo) => {
        state.nfo = nfo || { content: '', mtime: 0 };
        const textarea = el('library-film-dialog')?.querySelector('[data-lib-nfo]');
        if (textarea) textarea.value = nfo?.content || '';
        const status = el('library-film-dialog')?.querySelector('[data-lib-nfo-state]');
        if (status) status.textContent = '已重新读取';
      }).catch((error) => showToast(error.message, 'danger'));
      return;
    }
    if (event.target.closest('[data-lib-nfo-save]')) {
      if (!state.active) return;
      const textarea = el('library-film-dialog')?.querySelector('[data-lib-nfo]');
      const status = el('library-film-dialog')?.querySelector('[data-lib-nfo-state]');
      api(`/api/library/films/${encodeURIComponent(state.active.id)}/nfo`, {
        method: 'PUT',
        body: JSON.stringify({ content: textarea?.value || '', expected_mtime: Number(state.nfo?.mtime) || null }),
      }).then((payload) => {
        state.nfo = payload.nfo || state.nfo;
        if (status) status.textContent = '已保存原始 NFO';
        showToast('NFO 已保存');
        refresh();
      }).catch((error) => { if (status) status.textContent = error.message; });
      return;
    }
    if (event.target.closest('[data-lib-reveal]')) {
      const button = event.target.closest('[data-lib-reveal]');
      event.preventDefault();
      button.disabled = true;
      api(`/api/library/films/${encodeURIComponent(button.dataset.libReveal)}/reveal`, { method: 'POST' })
        .catch((error) => showToast(error.message || '无法打开文件夹', 'danger'))
        .finally(() => { button.disabled = false; });
      return;
    }
    if (event.target.closest('#library-film-save')) { saveFilm({ scrape: false }); return; }
    if (event.target.closest('#library-film-scrape')) { saveFilm({ scrape: true }); return; }
  });

  bindPlayerLifecycle();

  document.addEventListener('change', (event) => {
    if (event.target.id === 'library-sort') {
      state.sort = event.target.value;
      state.offset = 0;
      refresh();
      return;
    }
    if (event.target.id === 'library-group') {
      state.groupBy = event.target.value;
      state.group = null;
      state.groupsVisible = GROUP_PAGE;
      state.offset = 0;
      refresh();
    }
  });

  document.addEventListener('submit', (event) => {
    if (event.target.id === 'library-create-form') {
      event.preventDefault();
      createLibrary();
      return;
    }
    if (event.target.id === 'library-folder-form') {
      event.preventDefault();
      addFolder();
      return;
    }
    if (event.target.id === 'library-settings-form' || event.target.id === 'library-film-form') {
      event.preventDefault();
      return;
    }
    if (event.target.id !== 'library-page-search-form') return;
    event.preventDefault();
    state.query = String(el('library-page-search')?.value || '').trim();
    state.offset = 0;
    refresh();
  });

  window.JavspLibrary = {
    load,
    refresh,
    open: openFilm,
    scan,
    save: saveFilm,
    createLibrary,
    addFolder,
    removeFolder,
    selectLibrary,
    batchScrape,
    // 纯函数，供离线测试使用
    internals: { cardMarkup, statsMarkup, filterParams, detailMarkup, state, toneFor, tabsMarkup, foldersMarkup, groupCardsMarkup, groupCoverMarkup },

  };

  // 本脚本在 app.js 之后加载；刷新时若已停在媒体库页，需要自己触发一次加载
  if (document.querySelector('[data-panel="library"].active')) window.JavspLibrary.load();
})();
