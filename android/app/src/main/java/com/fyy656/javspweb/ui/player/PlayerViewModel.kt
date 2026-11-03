package com.fyy656.javspweb.ui.player

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.data.model.Film
import com.fyy656.javspweb.data.model.Playback
import com.fyy656.javspweb.ui.common.appViewModelFactory
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class PlayerUiState(
    val loading: Boolean = true,
    val error: String = "",
    val film: Film? = null,
    val playback: Playback? = null,
    val resolvedUrl: String = "",
    val isHls: Boolean = false,
    val startMs: Long = 0L,
    val title: String = "",
    // 剧集连播
    val seriesId: String = "",
    val season: Int = 0,
    val episodes: List<Film> = emptyList(),
    val currentIndex: Int = -1,
    val hasPrev: Boolean = false,
    val hasNext: Boolean = false,
)

class PlayerViewModel(private val app: JavspApp) : ViewModel() {
    private val repo = app.repository
    private val _state = MutableStateFlow(PlayerUiState())
    val state: StateFlow<PlayerUiState> = _state.asStateFlow()

    private var progressJob: Job? = null
    private var lastReportAt = 0L
    private var durationSec = 0.0
    private var stopped = false

    fun prepare(
        filmId: String,
        title: String,
        startSeconds: Int,
        seriesId: String = "",
        season: Int = 0,
        currentIndex: Int = -1,
    ) {
        stopped = false
        _state.update {
            it.copy(
                loading = true, error = "", title = title, seriesId = seriesId,
                season = season, episodes = emptyList(), currentIndex = currentIndex,
                hasPrev = false, hasNext = false,
            )
        }
        viewModelScope.launch {
            try {
                val film = repo.film(filmId)
                _state.update { it.copy(film = film, title = film.displayTitle) }
                // 剧集：拉同季分集用于上/下集。这里必须容错：
                // 分集列表拿不到不能影响“播放本集”这件事。
                if (seriesId.isNotBlank()) {
                    runCatching { loadEpisodes(seriesId, season, filmId, currentIndex) }
                }
                resolveAndPlay(film, startSeconds, forcePrefer = null)
            } catch (e: Exception) {
                _state.update { it.copy(loading = false, error = e.message ?: "无法加载影片") }
            }
        }
    }

    private suspend fun resolveAndPlay(film: Film, startSeconds: Int, forcePrefer: String?) {
        _state.update { it.copy(loading = true, error = "") }
        val playback = try {
            repo.playback(film.id, prefer = forcePrefer ?: "auto", start = startSeconds)
        } catch (e: Exception) {
            _state.update { it.copy(loading = false, error = e.message ?: "无法获取播放地址") }
            return
        }
        val url = repo.resolvePlaybackUrl(playback)
        val hls = playback.hls || url.substringBefore('?').endsWith(".m3u8", ignoreCase = true)
        _state.update {
            it.copy(
                loading = false,
                playback = playback,
                resolvedUrl = url,
                isHls = hls,
                startMs = startSeconds.coerceAtLeast(0) * 1000L,
                film = film,
            )
        }
        startProgressReporting()
    }

    /** 本地直连播放失败时，请求媒体服务器转码地址（fallback_mode=emby）。 */
    fun fallbackToTranscode() {
        val film = _state.value.film ?: return
        val start = (currentPositionSec()).coerceAtLeast(0)
        _state.update { it.copy(playback = null, resolvedUrl = "") }
        viewModelScope.launch { resolveAndPlay(film, start, forcePrefer = "emby") }
    }

    /** 手动切换直连 / 转码，与网页端一致：按钮上写的是要切换到的目标模式。 */
    fun switchPlaybackMode() {
        val film = _state.value.film ?: return
        val start = currentPositionSec().coerceAtLeast(0)
        val prefer = if (_state.value.playback?.isLocal == true) "emby" else "local"
        viewModelScope.launch { resolveAndPlay(film, start, forcePrefer = prefer) }
    }

    private var positionProvider: (() -> Long)? = null
    fun bindPositionProvider(provider: () -> Long) { positionProvider = provider }

    private fun currentPositionSec(): Int = ((positionProvider?.invoke() ?: 0L) / 1000L).toInt()

    private fun startProgressReporting() {
        progressJob?.cancel()
        progressJob = viewModelScope.launch {
            while (true) {
                delay(8000)
                reportPosition()
            }
        }
    }

    fun reportPosition() {
        val film = _state.value.film ?: return
        val posMs = positionProvider?.invoke() ?: return
        val position = posMs / 1000.0
        if (position <= 1) return
        val now = System.currentTimeMillis()
        if (now - lastReportAt < 5000) return
        lastReportAt = now
        viewModelScope.launch {
            runCatching { repo.saveProgress(film.id, position, durationSec) }
        }
    }

    fun setDuration(ms: Long) {
        durationSec = ms / 1000.0
    }

    fun playAdjacent(delta: Int) {
        val s = _state.value
        val nextIndex = s.currentIndex + delta
        if (nextIndex !in s.episodes.indices) return
        val ep = s.episodes[nextIndex]
        // 同季切集：不等 onCleared，直接结束当前转码并用新会话播放
        progressJob?.cancel()
        reportPositionNow()
        val current = s.playback
        val currentFilm = s.film
        if (currentFilm != null) {
            if (current != null && !current.isLocal) {
                viewModelScope.launch { runCatching { repo.stopPlayback(currentFilm.id, current.serverId, current.playSessionId) } }
            } else {
                viewModelScope.launch { runCatching { repo.stopPlayback(currentFilm.id, "", "") } }
            }
        }
        stopped = false
        prepare(
            filmId = ep.id,
            title = ep.displayTitle,
            startSeconds = 0,
            seriesId = s.seriesId,
            season = s.season,
            currentIndex = nextIndex,
        )
    }

    /** 关闭/切集时通知服务端停掉转码并保存进度。 */
    fun stopPlayback(reportProgress: Boolean = true) {
        if (stopped) return
        stopped = true
        val s = _state.value
        val film = s.film
        val playback = s.playback
        progressJob?.cancel()
        if (reportProgress) reportPositionNow()
        if (film != null && playback != null && !playback.isLocal) {
            viewModelScope.launch {
                runCatching { repo.stopPlayback(film.id, playback.serverId, playback.playSessionId) }
            }
        } else if (film != null) {
            viewModelScope.launch { runCatching { repo.stopPlayback(film.id, "", "") } }
        }
    }

    private fun reportPositionNow() {
        val film = _state.value.film ?: return
        val position = (positionProvider?.invoke() ?: 0L) / 1000.0
        if (position <= 1) return
        viewModelScope.launch {
            runCatching { repo.saveProgress(film.id, position, durationSec) }
        }
    }

    override fun onCleared() {
        stopPlayback(reportProgress = true)
        super.onCleared()
    }

    /** 服务端 limit 上限 200，分集多时逐页取全。失败不抛出，只退化为无上/下集。 */
    private suspend fun loadEpisodes(seriesId: String, season: Int, filmId: String, currentIndex: Int) {
        val all = mutableListOf<Film>()
        var offset = 0
        while (true) {
            val page = repo.films(parent = seriesId, season = season, limit = EPISODE_PAGE, offset = offset)
            all += page.items
            offset += page.items.size
            if (page.items.isEmpty() || offset >= page.total) break
        }
        val list = all.distinctBy { it.id }
            .sortedWith(compareBy({ it.season }, { it.episode }, { it.title }))
        val idx = currentIndex.takeIf { it in list.indices } ?: list.indexOfFirst { it.id == filmId }
        _state.update {
            it.copy(
                episodes = list, currentIndex = idx,
                hasPrev = idx > 0,
                hasNext = idx in 0 until list.size - 1,
            )
        }
    }

    companion object {
        private const val EPISODE_PAGE = 200
        val Factory = appViewModelFactory { app: JavspApp -> PlayerViewModel(app) }
    }
}
