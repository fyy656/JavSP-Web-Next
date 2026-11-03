package com.fyy656.javspweb.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class LoginRequest(
    val username: String,
    val password: String,
)

@Serializable
data class MeResponse(
    val username: String = "",
    val role: String = "operator",
)

@Serializable
data class Folder(
    val id: String = "",
    val path: String = "",
    val name: String = "",
    val recursive: Boolean = true,
    @SerialName("film_count") val filmCount: Int = 0,
    @SerialName("last_scan_at") val lastScanAt: String? = null,
    @SerialName("scan_status") val scanStatus: String? = null,
)

@Serializable
data class LibraryStats(
    val total: Int = 0,
    val complete: Int = 0,
    val partial: Int = 0,
    val unscraped: Int = 0,
    val failed: Int = 0,
    val missing: Int = 0,
    val scraping: Int = 0,
)

@Serializable
data class Library(
    val id: String,
    val name: String = "",
    val kind: String = "movie",
    @SerialName("preset_id") val presetId: String = "default",
    val folders: List<Folder> = emptyList(),
    @SerialName("film_count") val filmCount: Int = 0,
    @SerialName("scan_status") val scanStatus: String? = null,
    @SerialName("last_scan_at") val lastScanAt: String? = null,
    val stats: LibraryStats = LibraryStats(),
)

@Serializable
data class LibrariesResponse(val libraries: List<Library> = emptyList())

@Serializable
data class Progress(
    val position: Double = 0.0,
    val duration: Double = 0.0,
    val finished: Boolean = false,
    val percent: Double = 0.0,
    @SerialName("updated_at") val updatedAt: String = "",
)

@Serializable
data class Film(
    val id: String,
    val kind: String = "movie",
    @SerialName("parent_id") val parentId: String = "",
    val season: Int = 0,
    val episode: Int = 0,
    val dvdid: String = "",
    val title: String = "",
    @SerialName("original_title") val originalTitle: String = "",
    val plot: String = "",
    @SerialName("release_date") val releaseDate: String = "",
    val studio: String = "",
    val publisher: String = "",
    val director: String = "",
    val actress: List<String> = emptyList(),
    val runtime: Int = 0,
    @SerialName("size_bytes") val sizeBytes: Long = 0L,
    @SerialName("dir_path") val dirPath: String = "",
    @SerialName("video_path") val videoPath: String = "",
    @SerialName("cover_path") val coverPath: String = "",
    @SerialName("fanart_count") val fanartCount: Int = 0,
    val status: String = "unscraped",
    @SerialName("missing_fields") val missingFields: List<String> = emptyList(),
    @SerialName("scrape_state") val scrapeState: String = "idle",
    @SerialName("last_task_id") val lastTaskId: String = "",
    @SerialName("last_error") val lastError: String = "",
    @SerialName("updated_at") val updatedAt: String = "",
    val progress: Progress = Progress(),
    @SerialName("episode_count") val episodeCount: Int = 0,
    @SerialName("season_count") val seasonCount: Int = 0,
) {
    val isSeries: Boolean get() = kind == "series"
    val isEpisode: Boolean get() = kind == "episode"
    val isScraping: Boolean get() = scrapeState == "queued" || scrapeState == "running"
    val displayTitle: String
        get() = title.ifBlank {
            videoPath.substringAfterLast('/').substringAfterLast('\\').substringBeforeLast('.')
        }
}

@Serializable
data class FilmsResponse(
    val items: List<Film> = emptyList(),
    val total: Int = 0,
    val limit: Int = 48,
    val offset: Int = 0,
    val stats: LibraryStats = LibraryStats(),
)

@Serializable
data class Season(
    val season: Int = 0,
    val episodes: Int = 0,
)

@Serializable
data class ResumeEpisode(
    @SerialName("film_id") val filmId: String = "",
    val season: Int = 0,
    val episode: Int = 0,
    val title: String = "",
    val position: Double = 0.0,
    val duration: Double = 0.0,
)

@Serializable
data class SeriesProgress(
    val total: Int = 0,
    val watched: Int = 0,
    val resume: ResumeEpisode? = null,
)

@Serializable
data class SeriesSeasonsResponse(
    val seasons: List<Season> = emptyList(),
    val episodes: Int = 0,
    val progress: SeriesProgress = SeriesProgress(),
)

@Serializable
data class FilmResponse(val film: Film)

@Serializable
data class Scan(
    val id: String,
    @SerialName("library_id") val libraryId: String = "",
    val status: String = "queued",
    val discovered: Int = 0,
    val added: Int = 0,
    val updated: Int = 0,
    val removed: Int = 0,
    val skipped: Int = 0,
    val error: String = "",
    @SerialName("started_at") val startedAt: String = "",
    @SerialName("finished_at") val finishedAt: String? = null,
) {
    val isFinished: Boolean get() = status == "succeeded" || status == "failed"
}

@Serializable
data class ScanResponse(val scan: Scan)

@Serializable
data class ScrapeResponse(
    @SerialName("task_id") val taskId: String = "",
    val film: Film? = null,
)

@Serializable
data class BatchScrapeRequest(
    @SerialName("library_id") val libraryId: String = "",
    @SerialName("folder_id") val folderId: String = "",
    val status: String = "",
    val query: String = "",
    @SerialName("group_by") val groupBy: String = "",
    val group: String = "",
    val limit: Int = 10000,
    val ids: List<String>? = null,
    @SerialName("preset_id") val presetId: String? = null,
)

@Serializable
data class SkippedItem(
    val id: String = "",
    val reason: String = "",
)

@Serializable
data class BatchScrapeResponse(
    val accepted: List<String> = emptyList(),
    val skipped: List<SkippedItem> = emptyList(),
    val total: Int = 0,
)

@Serializable
data class ScrapeRequest(
    val dvdid: String? = null,
    @SerialName("preset_id") val presetId: String? = null,
)

@Serializable
data class Playback(
    val mode: String = "local",
    @SerialName("server_id") val serverId: String = "",
    @SerialName("server_name") val serverName: String = "",
    @SerialName("item_id") val itemId: String = "",
    val url: String = "",
    @SerialName("fallback_url") val fallbackUrl: String = "",
    @SerialName("fallback_mode") val fallbackMode: String = "",
    val container: String = "",
    val transcoding: Boolean = false,
    val hls: Boolean = false,
    @SerialName("start_seconds") val startSeconds: Int = 0,
    @SerialName("play_session_id") val playSessionId: String = "",
) {
    val isLocal: Boolean get() = mode == "local"
}

@Serializable
data class ProgressRequest(
    val position: Double,
    val duration: Double,
)

@Serializable
data class PlaybackStopRequest(
    @SerialName("server_id") val serverId: String = "",
    @SerialName("play_session_id") val playSessionId: String = "",
)

@Serializable
data class Preset(
    val id: String,
    val name: String = "",
)

@Serializable
data class CoverRef(
    val id: String = "",
    val v: String = "",
)

@Serializable
data class GroupItem(
    val key: String = "",
    val name: String = "",
    val count: Int = 0,
    val covers: List<CoverRef> = emptyList(),
)

@Serializable
data class GroupsResponse(
    val by: String = "actress",
    val groups: List<GroupItem> = emptyList(),
    val total: Int = 0,
)

@Serializable
data class FilmUpdateRequest(
    val dvdid: String = "",
    val title: String = "",
    @SerialName("original_title") val originalTitle: String = "",
    val plot: String = "",
    @SerialName("release_date") val releaseDate: String = "",
    val studio: String = "",
    val publisher: String = "",
    val director: String = "",
    val runtime: Int = 0,
    val actress: List<String> = emptyList(),
    @SerialName("write_nfo") val writeNfo: Boolean = true,
)
