package com.fyy656.javspweb.data

import com.fyy656.javspweb.data.api.ApiClient
import com.fyy656.javspweb.data.model.BatchScrapeRequest
import com.fyy656.javspweb.data.model.BatchScrapeResponse
import com.fyy656.javspweb.data.model.Film
import com.fyy656.javspweb.data.model.FilmUpdateRequest
import com.fyy656.javspweb.data.model.FilmsResponse
import com.fyy656.javspweb.data.model.GroupsResponse
import com.fyy656.javspweb.data.model.Library
import com.fyy656.javspweb.data.model.MeResponse
import com.fyy656.javspweb.data.model.Playback
import com.fyy656.javspweb.data.model.PlaybackStopRequest
import com.fyy656.javspweb.data.model.Preset
import com.fyy656.javspweb.data.model.ProgressRequest
import com.fyy656.javspweb.data.model.Scan
import com.fyy656.javspweb.data.model.ScrapeRequest
import com.fyy656.javspweb.data.model.ScrapeResponse
import com.fyy656.javspweb.data.model.SeriesSeasonsResponse

class JavspRepository(private val client: ApiClient) {

    suspend fun login(username: String, password: String): MeResponse =
        apiCall { client.api.login(com.fyy656.javspweb.data.model.LoginRequest(username, password)) }

    suspend fun me(): MeResponse = apiCall { client.api.me() }

    suspend fun logout(): Unit = apiCall { client.api.logout() }

    suspend fun libraries(): List<Library> = apiCall { client.api.libraries() }.libraries

    suspend fun films(
        libraryId: String = "",
        status: String = "",
        query: String = "",
        kind: String = "",
        groupBy: String = "",
        group: String = "",
        parent: String = "",
        season: Int? = null,
        sort: String = "updated",
        direction: String = "desc",
        limit: Int = 48,
        offset: Int = 0,
    ): FilmsResponse = apiCall {
        client.api.films(
            libraryId = libraryId,
            status = status,
            query = query,
            kind = kind,
        groupBy = groupBy,
        group = group,
            parent = parent,
            season = season,
            sort = sort,
            direction = direction,
            limit = limit,
            offset = offset,
        )
    }

    suspend fun film(id: String): Film = apiCall { client.api.film(id) }.film

    suspend fun seriesSeasons(id: String): SeriesSeasonsResponse =
        apiCall { client.api.seriesSeasons(id) }

    suspend fun startScan(libraryId: String): Scan =
        apiCall { client.api.scan(libraryId) }.scan

    suspend fun scanStatus(scanId: String): Scan =
        apiCall { client.api.scanStatus(scanId) }.scan

    suspend fun scrapeFilm(filmId: String, dvdid: String? = null, presetId: String? = null): ScrapeResponse =
        apiCall { client.api.scrapeFilm(filmId, ScrapeRequest(dvdid, presetId)) }

    suspend fun batchScrape(request: BatchScrapeRequest): BatchScrapeResponse =
        apiCall { client.api.batchScrape(request) }

    suspend fun updateFilm(filmId: String, body: FilmUpdateRequest): Film =
        apiCall { client.api.updateFilm(filmId, body) }.film

    suspend fun groups(by: String, libraryId: String = "", query: String = ""): GroupsResponse =
        apiCall { client.api.groups(by, libraryId, query) }

    suspend fun playback(filmId: String, prefer: String = "auto", start: Int = 0): Playback =
        apiCall { client.api.playback(filmId, prefer, start) }

    suspend fun stopPlayback(filmId: String, serverId: String, sessionId: String) {
        apiCall<Unit> {
            client.api.stopPlayback(filmId, PlaybackStopRequest(serverId, sessionId))
        }
    }

    suspend fun saveProgress(filmId: String, position: Double, duration: Double) {
        apiCall<Unit> { client.api.saveProgress(filmId, ProgressRequest(position, duration)) }
    }

    suspend fun presets(): List<Preset> = apiCall { client.api.presets() }

    /** 封面走服务端接口，自动携带会话 Cookie。 */
    fun coverUrl(filmId: String, updatedAt: String = ""): String {
        val suffix = if (updatedAt.isNotBlank()) "?v=" + java.net.URLEncoder.encode(updatedAt, "UTF-8") else ""
        return client.resolveUrl("api/library/films/$filmId/cover$suffix")
    }

    /** 把 playback 里的相对本地流地址补成绝对地址；Emby 地址已是绝对地址。 */
    fun resolvePlaybackUrl(playback: Playback): String = client.resolveUrl(playback.url)
}
