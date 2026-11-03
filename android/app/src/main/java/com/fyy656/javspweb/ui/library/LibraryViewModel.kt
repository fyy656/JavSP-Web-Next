package com.fyy656.javspweb.ui.library

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.data.ApiException
import com.fyy656.javspweb.data.model.BatchScrapeRequest
import com.fyy656.javspweb.data.model.Film
import com.fyy656.javspweb.data.model.GroupItem
import com.fyy656.javspweb.data.model.Library
import com.fyy656.javspweb.data.model.Scan
import com.fyy656.javspweb.ui.common.appViewModelFactory
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

enum class FilmStatusFilter(val key: String, val label: String) {
    ALL("", "全部"),
    COMPLETE("complete", "资料完整"),
    PARTIAL("partial", "缺资料"),
    UNSCRAPED("unscraped", "未刮削"),
    FAILED("failed", "刮削失败"),
    MISSING("missing", "文件丢失"),
}

data class LibraryUiState(
    val loading: Boolean = false,
    val refreshing: Boolean = false,
    val libraries: List<Library> = emptyList(),
    val selectedLibraryId: String? = null,
    val statusFilter: FilmStatusFilter = FilmStatusFilter.ALL,
    val sort: String = "updated",
    val query: String = "",
    // 分类浏览：groupBy 为空表示不分类；groupKey 为空表示正看分类列表
    val groupBy: String = "",
    val groups: List<GroupItem> = emptyList(),
    val groupsLoading: Boolean = false,
    val groupKey: String? = null,
    val items: List<Film> = emptyList(),
    val total: Int = 0,
    val offset: Int = 0,
    val hasMore: Boolean = false,
    val appendLoading: Boolean = false,
    val scanning: Boolean = false,
    val scan: Scan? = null,
    val message: String = "",
    val error: String = "",
    val loggedOut: Boolean = false,
    val isAdmin: Boolean = false,
)

private const val PAGE_SIZE = 48

class LibraryViewModel(private val app: JavspApp) : ViewModel() {
    private val repo = app.repository
    private val _state = MutableStateFlow(LibraryUiState())
    val state: StateFlow<LibraryUiState> = _state.asStateFlow()

    private var pollJob: Job? = null
    private var searchJob: Job? = null

    init {
        bootstrap()
    }

    private fun bootstrap() {
        viewModelScope.launch {
            try {
                val me = runCatching { repo.me() }.getOrElse {
                    _state.update { it.copy(loggedOut = true) }
                    return@launch
                }
                app.setRole(me.role)
                _state.update { it.copy(isAdmin = app.isAdmin) }
                val libs = repo.libraries()
                _state.update { it.copy(libraries = libs, selectedLibraryId = it.selectedLibraryId ?: libs.firstOrNull()?.id) }
                loadFirstPage()
            } catch (e: Exception) {
                if (isUnauthorized(e)) {
                    _state.update { it.copy(loggedOut = true, loading = false) }
                    return@launch
                }
                _state.update { it.copy(error = e.message ?: "加载失败", loading = false) }
            }
        }
    }

    fun consumeMessage() = _state.update { it.copy(message = "") }
    fun consumeError() = _state.update { it.copy(error = "") }

    fun selectLibrary(id: String?) {
        _state.update {
            it.copy(selectedLibraryId = id, statusFilter = FilmStatusFilter.ALL, query = "",
                groupBy = "", groupKey = null, groups = emptyList())
        }
        loadFirstPage()
    }

    /** 切换分类维度；传空串表示关闭分类。 */
    fun setGroupBy(by: String) {
        val s = _state.value
        if (s.groupBy == by && by.isNotBlank()) return
        _state.update { it.copy(groupBy = by, groupKey = null, groups = emptyList()) }
        if (by.isBlank()) loadFirstPage() else loadGroups()
    }

    /** 点进某个分类，看它的影片。 */
    fun openGroup(key: String) {
        _state.update { it.copy(groupKey = key) }
        loadFirstPage()
    }

    /** 从分类影片返回分类列表。 */
    fun backToGroups() {
        _state.update { it.copy(groupKey = null, items = emptyList(), total = 0) }
        loadGroups()
    }

    private fun loadGroups() {
        _state.update { it.copy(groupsLoading = true, error = "") }
        viewModelScope.launch {
            try {
                val s = _state.value
                val result = repo.groups(s.groupBy, s.selectedLibraryId ?: "", s.query.trim())
                _state.update { it.copy(groupsLoading = false, groups = result.groups) }
            } catch (e: Exception) {
                if (isUnauthorized(e)) {
                    _state.update { it.copy(groupsLoading = false, loggedOut = true) }
                    return@launch
                }
                _state.update { it.copy(groupsLoading = false, error = e.message ?: "分类加载失败") }
            }
        }
    }

    fun setStatusFilter(filter: FilmStatusFilter) {
        _state.update { it.copy(statusFilter = filter) }
        loadFirstPage()
    }

    fun setSort(sort: String) {
        if (sort == _state.value.sort) return
        _state.update { it.copy(sort = sort) }
        loadFirstPage()
    }

    /** 输入防抖：停止输入 350ms 后再请求，避免每敲一个字就翻页。 */
    fun setQuery(q: String) {
        _state.update { it.copy(query = q) }
        searchJob?.cancel()
        searchJob = viewModelScope.launch {
            delay(350)
            if (_state.value.groupBy.isNotBlank() && _state.value.groupKey == null) loadGroups() else loadFirstPage()
        }
    }

    fun submitQuery() {
        searchJob?.cancel()
        loadFirstPage()
    }

    private fun currentLibrary(): Library? {
        val id = _state.value.selectedLibraryId ?: return null
        return _state.value.libraries.firstOrNull { it.id == id }
    }

    /**
     * 影片墙的 kind 规则与服务端一致：
     * - 剧集库只列 series；点进去再看季/分集
     * - 其余（成人/电影）列非 episode 条目
     */
    private fun queryKind(lib: Library?): String = if (lib?.kind == "series") "series" else ""

    private fun loadFirstPage() {
        pollJob?.cancel()
        _state.update { it.copy(loading = true, error = "") }
        viewModelScope.launch {
            try {
                val lib = currentLibrary()
                val s = _state.value
                val result = repo.films(
                    libraryId = s.selectedLibraryId ?: "",
                    status = s.statusFilter.key,
                    query = s.query.trim(),
                    kind = queryKind(lib),
                    groupBy = s.groupBy,
                    group = s.groupKey ?: "",
                    sort = s.sort,
                    limit = PAGE_SIZE,
                    offset = 0,
                )
                _state.update {
                    it.copy(
                        loading = false,
                        items = result.items,
                        total = result.total,
                        offset = result.items.size,
                        hasMore = result.items.size < result.total,
                    )
                }
                maybeStartScrapePolling()
            } catch (e: Exception) {
                if (isUnauthorized(e)) {
                    _state.update { it.copy(loggedOut = true, loading = false, appendLoading = false) }
                    return@launch
                }
                _state.update { it.copy(loading = false, error = e.message ?: "加载失败") }
            }
        }
    }

    private fun isUnauthorized(e: Exception): Boolean = e is ApiException && e.code == 401

    fun loadNextPage() {
        val s = _state.value
        if (s.appendLoading || !s.hasMore) return
        _state.update { it.copy(appendLoading = true) }
        viewModelScope.launch {
            try {
                val lib = currentLibrary()
                val result = repo.films(
                    libraryId = s.selectedLibraryId ?: "",
                    status = s.statusFilter.key,
                    query = s.query.trim(),
                    kind = queryKind(lib),
                    groupBy = s.groupBy,
                    group = s.groupKey ?: "",
                    sort = s.sort,
                    limit = PAGE_SIZE,
                    offset = s.offset,
                )
                _state.update {
                    val merged = (it.items + result.items).distinctBy { film -> film.id }
                    it.copy(
                        appendLoading = false,
                        items = merged,
                        total = result.total,
                        offset = merged.size,
                        hasMore = merged.size < result.total,
                    )
                }
            } catch (e: Exception) {
                _state.update { it.copy(appendLoading = false, message = e.message ?: "加载更多失败") }
            }
        }
    }

    fun refresh(silent: Boolean = false) {
        if (_state.value.scanning) return
        _state.update { it.copy(refreshing = !silent) }
        viewModelScope.launch {
            try {
                val libs = repo.libraries()
                _state.update { it.copy(libraries = libs) }
                val lib = currentLibrary()
                val s = _state.value
                val result = repo.films(
                    libraryId = s.selectedLibraryId ?: "",
                    status = s.statusFilter.key,
                    query = s.query.trim(),
                    kind = queryKind(lib),
                    groupBy = s.groupBy,
                    group = s.groupKey ?: "",
                    sort = s.sort,
                    limit = s.offset.coerceIn(PAGE_SIZE, 200),
                    offset = 0,
                )
                _state.update {
                    it.copy(
                        refreshing = false,
                        items = result.items,
                        total = result.total,
                        offset = result.items.size,
                        hasMore = result.items.size < result.total,
                    )
                }
                maybeStartScrapePolling()
            } catch (e: Exception) {
                _state.update { it.copy(refreshing = false, message = e.message ?: "刷新失败") }
            }
        }
    }

    // ---------------------------------------------------------------- 扫描
    fun startScan() {
        val lib = currentLibrary() ?: run {
            _state.update { it.copy(message = "请先选择一个媒体库") }
            return
        }
        viewModelScope.launch {
            try {
                val scan = repo.startScan(lib.id)
                _state.update { it.copy(scanning = true, scan = scan, message = "正在扫描…") }
                pollScan(scan.id)
            } catch (e: Exception) {
                _state.update { it.copy(message = e.message ?: "扫描启动失败") }
            }
        }
    }

    private fun pollScan(scanId: String) {
        viewModelScope.launch {
            var guard = 0
            while (isActive && guard < 1000) {
                guard++
                delay(800)
                val scan = try {
                    repo.scanStatus(scanId)
                } catch (e: Exception) {
                    null
                }
                if (scan != null) {
                    _state.update { it.copy(scan = scan) }
                    if (scan.isFinished) {
                        _state.update {
                            it.copy(
                                scanning = false,
                                message = if (scan.status == "failed")
                                    "扫描失败：${scan.error}"
                                else
                                    "扫描完成：发现 ${scan.discovered}，新增 ${scan.added}，更新 ${scan.updated}，移除 ${scan.removed}",
                            )
                        }
                        refresh(silent = true)
                        break
                    }
                }
            }
        }
    }

    // ---------------------------------------------------------------- 刮削
    fun batchScrapeCurrent() {
        val s = _state.value
        val lib = currentLibrary()
        viewModelScope.launch {
            try {
                val result = repo.batchScrape(
                    BatchScrapeRequest(
                        libraryId = s.selectedLibraryId ?: "",
                        status = s.statusFilter.key,
                        query = s.query.trim(),
                        presetId = lib?.presetId?.ifBlank { null },
                        limit = 5000,
                    )
                )
                _state.update {
                    it.copy(
                        message = if (result.skipped.isNotEmpty())
                            "已加入 ${result.accepted.size} 个，跳过 ${result.skipped.size} 个"
                        else "已加入 ${result.accepted.size} 个刮削任务",
                    )
                }
                refresh(silent = true)
            } catch (e: Exception) {
                _state.update { it.copy(message = e.message ?: "批量刮削失败") }
            }
        }
    }

    /** 有 queued/running 的影片时每 4 秒静默刷新，完成即停止。 */
    private fun maybeStartScrapePolling() {
        val anyActive = _state.value.items.any { it.isScraping }
        if (anyActive && pollJob?.isActive != true) {
            pollJob = viewModelScope.launch {
                while (isActive) {
                    delay(4000)
                    refresh(silent = true)
                    if (!_state.value.items.any { it.isScraping }) break
                }
            }
        } else if (!anyActive && pollJob?.isActive == true) {
            pollJob?.cancel()
        }
    }

    fun logout(onDone: () -> Unit) {
        viewModelScope.launch {
            runCatching { repo.logout() }
            app.cookieJar.clear()
            // 保留服务器地址，重连时只需重新登录
            onDone()
        }
    }

    override fun onCleared() {
        pollJob?.cancel()
        searchJob?.cancel()
        super.onCleared()
    }

    companion object {
        val Factory = appViewModelFactory { app: JavspApp -> LibraryViewModel(app) }
    }
}
