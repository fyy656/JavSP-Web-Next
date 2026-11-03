package com.fyy656.javspweb.data.api

import com.fyy656.javspweb.data.model.BatchScrapeRequest
import com.fyy656.javspweb.data.model.BatchScrapeResponse
import com.fyy656.javspweb.data.model.FilmResponse
import com.fyy656.javspweb.data.model.FilmUpdateRequest
import com.fyy656.javspweb.data.model.FilmsResponse
import com.fyy656.javspweb.data.model.GroupsResponse
import com.fyy656.javspweb.data.model.LibrariesResponse
import com.fyy656.javspweb.data.model.LoginRequest
import com.fyy656.javspweb.data.model.MeResponse
import com.fyy656.javspweb.data.model.Playback
import com.fyy656.javspweb.data.model.PlaybackStopRequest
import com.fyy656.javspweb.data.model.Preset
import com.fyy656.javspweb.data.model.ProgressRequest
import com.fyy656.javspweb.data.model.ScanResponse
import com.fyy656.javspweb.data.model.ScrapeRequest
import com.fyy656.javspweb.data.model.ScrapeResponse
import com.fyy656.javspweb.data.model.SeriesSeasonsResponse
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query

interface JavspApi {
    @POST("api/auth/login")
    suspend fun login(@Body body: LoginRequest): Response<MeResponse>

    @GET("api/auth/me")
    suspend fun me(): Response<MeResponse>

    @POST("api/auth/logout")
    suspend fun logout(): Response<Unit>

    @GET("api/library/libraries")
    suspend fun libraries(): Response<LibrariesResponse>

    @GET("api/library/films")
    suspend fun films(
        @Query("library_id") libraryId: String = "",
        @Query("folder_id") folderId: String = "",
        @Query("status") status: String = "",
        @Query("query") query: String = "",
        @Query("sort") sort: String = "updated",
        @Query("direction") direction: String = "desc",
        @Query("kind") kind: String = "",
        @Query("group_by") groupBy: String = "",
        @Query("group") group: String = "",
        @Query("parent") parent: String = "",
        @Query("season") season: Int? = null,
        @Query("limit") limit: Int = 48,
        @Query("offset") offset: Int = 0,
    ): Response<FilmsResponse>

    @GET("api/library/groups")
    suspend fun groups(
        @Query("by") by: String,
        @Query("library_id") libraryId: String = "",
        @Query("query") query: String = "",
    ): Response<GroupsResponse>

    @GET("api/library/films/{id}")
    suspend fun film(@Path("id") id: String): Response<FilmResponse>

    @PATCH("api/library/films/{id}")
    suspend fun updateFilm(
        @Path("id") id: String,
        @Body body: FilmUpdateRequest,
    ): Response<FilmResponse>

    @GET("api/library/series/{id}/seasons")
    suspend fun seriesSeasons(@Path("id") id: String): Response<SeriesSeasonsResponse>

    @POST("api/library/libraries/{id}/scan")
    suspend fun scan(@Path("id") id: String): Response<ScanResponse>

    @GET("api/library/scans/{id}")
    suspend fun scanStatus(@Path("id") id: String): Response<ScanResponse>

    @POST("api/library/films/{id}/scrape")
    suspend fun scrapeFilm(
        @Path("id") id: String,
        @Body body: ScrapeRequest = ScrapeRequest(),
    ): Response<ScrapeResponse>

    @POST("api/library/films/scrape")
    suspend fun batchScrape(@Body body: BatchScrapeRequest): Response<BatchScrapeResponse>

    @GET("api/library/films/{id}/playback")
    suspend fun playback(
        @Path("id") id: String,
        @Query("prefer") prefer: String = "auto",
        @Query("start") start: Int = 0,
    ): Response<Playback>

    @POST("api/library/films/{id}/playback/stop")
    suspend fun stopPlayback(
        @Path("id") id: String,
        @Body body: PlaybackStopRequest = PlaybackStopRequest(),
    ): Response<Unit>

    @PUT("api/library/films/{id}/progress")
    suspend fun saveProgress(
        @Path("id") id: String,
        @Body body: ProgressRequest,
    ): Response<Unit>

    @GET("api/presets")
    suspend fun presets(): Response<List<Preset>>
}
