package com.fyy656.javspweb.ui.detail

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.data.model.Film
import com.fyy656.javspweb.data.model.FilmUpdateRequest
import com.fyy656.javspweb.data.model.Season
import com.fyy656.javspweb.data.model.SeriesProgress
import com.fyy656.javspweb.ui.common.appViewModelFactory
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class DetailUiState(
    val loading: Boolean = true,
    val film: Film? = null,
    val seasons: List<Season> = emptyList(),
    val seasonFocus: Int = 1,
    val episodes: List<Film> = emptyList(),
    val episodesLoading: Boolean = false,
    val seriesProgress: SeriesProgress? = null,
    val scraping: Boolean = false,
    val saving: Boolean = false,
    val dvdIdInput: String = "",
    val editingId: Boolean = false,
    val message: String = "",
    val error: String = "",
)

class DetailViewModel(private val app: JavspApp) : ViewModel() {
    private val repo = app.repository
    private val _state = MutableStateFlow(DetailUiState())
    val state: StateFlow<DetailUiState> = _state.asStateFlow()
    private var pollJob: Job? = null

    fun load(filmId: String) {
        _state.update { it.copy(loading = true, error = "") }
        viewModelScope.launch {
            try {
                val film = repo.film(filmId)
                _state.update { it.copy(loading = false, film = film, dvdIdInput = film.dvdid) }
                if (film.isSeries) {
                    val info = repo.seriesSeasons(filmId)
                    // 有续看记录时直接定位到那一季，保证分集列表里能找到它
                    val resumeSeason = info.progress.resume?.season
                    val firstSeason = resumeSeason
                        ?: info.seasons.firstOrNull()?.season
                        ?: 1
                    _state.update {
                        it.copy(
                            seasons = info.seasons,
                            seasonFocus = firstSeason,
                            seriesProgress = info.progress,
                        )
                    }
                    loadEpisodes(filmId, firstSeason)
                }
            } catch (e: Exception) {
                _state.update { it.copy(loading = false, error = e.message ?: "加载失败") }
            }
        }
    }

    fun selectSeason(season: Int) {
        val film = _state.value.film ?: return
        _state.update { it.copy(seasonFocus = season) }
        loadEpisodes(film.id, season)
    }

    private fun loadEpisodes(seriesId: String, season: Int) {
        _state.update { it.copy(episodesLoading = true) }
        viewModelScope.launch {
            try {
                // 服务端 limit 上限 200，分集多时逐页取全
                val all = mutableListOf<com.fyy656.javspweb.data.model.Film>()
                var offset = 0
                while (true) {
                    val page = repo.films(
                        parent = seriesId,
                        season = season,
                        limit = EPISODE_PAGE,
                        offset = offset,
                    )
                    all += page.items
                    offset += page.items.size
                    if (page.items.isEmpty() || offset >= page.total) break
                }
                val sorted = all.distinctBy { it.id }
                    .sortedWith(compareBy({ it.season }, { it.episode }, { it.title }))
                _state.update { it.copy(episodesLoading = false, episodes = sorted) }
                if (sorted.isEmpty()) {
                    _state.update { it.copy(message = "这一季没有读到分集，可在列表页重新扫描媒体库") }
                }
            } catch (e: Exception) {
                _state.update { it.copy(episodesLoading = false, message = e.message ?: "分集加载失败") }
            }
        }
    }

    fun consumeMessage() = _state.update { it.copy(message = "") }

    fun reportMessage(text: String) = _state.update { it.copy(message = text) }

    /** 保存资料并写入 NFO（服务端按电影/剧集/分集写对应根节点）。 */
    fun save(body: FilmUpdateRequest, onDone: () -> Unit = {}) {
        val film = _state.value.film ?: return
        if (_state.value.saving) return
        _state.update { it.copy(saving = true) }
        viewModelScope.launch {
            try {
                val updated = repo.updateFilm(film.id, body)
                _state.update { it.copy(film = updated, saving = false, message = "资料已保存") }
                onDone()
            } catch (e: Exception) {
                _state.update { it.copy(saving = false, message = e.message ?: "保存失败") }
            }
        }
    }

    fun setEditingId(editing: Boolean) {
        _state.update { it.copy(editingId = editing, dvdIdInput = it.film?.dvdid ?: "") }
    }

    fun onDvdId(v: String) = _state.update { it.copy(dvdIdInput = v) }

    /** 单片刮削；剧集库不支持联网刮削（服务端会拒绝）。 */
    fun scrapeCurrent(dvdidOverride: String? = null) {
        val film = _state.value.film ?: return
        viewModelScope.launch {
            try {
                val override = dvdidOverride?.trim()?.ifBlank { null }
                repo.scrapeFilm(film.id, dvdid = override, presetId = null)
                _state.update { it.copy(editingId = false, message = "已开始刮削 ${override ?: film.dvdid}") }
                startPolling(film.id)
            } catch (e: Exception) {
                _state.update { it.copy(message = e.message ?: "无法开始刮削") }
            }
        }
    }

    fun startScan() {
        // 详情页不提供扫描
    }

    private fun startPolling(filmId: String) {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (isActive) {
                delay(4000)
                try {
                    val fresh = repo.film(filmId)
                    _state.update { it.copy(film = fresh) }
                    if (!fresh.isScraping) {
                        _state.update {
                            it.copy(
                                message = if (fresh.scrapeState == "failed")
                                    "刮削失败：${fresh.lastError.ifBlank { "未知原因" }}" else "刮削完成",
                            )
                        }
                        break
                    }
                } catch (_: Exception) {
                    break
                }
            }
        }
    }

    /** 进入页面时若已在刮削，恢复轮询。 */
    fun ensurePollingIfActive() {
        val film = _state.value.film ?: return
        if (film.isScraping && pollJob?.isActive != true) startPolling(film.id)
    }

    override fun onCleared() {
        pollJob?.cancel()
        super.onCleared()
    }

    companion object {
        private const val EPISODE_PAGE = 200
        val Factory = appViewModelFactory { app: JavspApp -> DetailViewModel(app) }
    }
}
