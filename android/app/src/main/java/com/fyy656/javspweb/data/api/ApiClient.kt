package com.fyy656.javspweb.data.api

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import java.util.concurrent.TimeUnit

/** 单一 OkHttp/Retrofit 实例，服务器地址可变时整体重建；CookieJar 保留会话。 */
class ApiClient(val cookieJar: SessionCookieJar = SessionCookieJar()) {

    val json: Json = Json {
        ignoreUnknownKeys = true
        coerceInputValues = true
        explicitNulls = false
    }

    @Volatile var baseUrl: String = ""
        private set

    val httpClient: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .addInterceptor(HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        })
        .build()

    @Volatile private var retrofitApi: JavspApi? = null

    fun setBaseUrl(raw: String) {
        val normalized = normalizeBaseUrl(raw)
        if (normalized == baseUrl && retrofitApi != null) return
        synchronized(this) {
            baseUrl = normalized
            val contentType = "application/json".toMediaType()
            retrofitApi = Retrofit.Builder()
                .baseUrl(normalized)
                .client(httpClient)
                .addConverterFactory(json.asConverterFactory(contentType))
                .build()
                .create(JavspApi::class.java)
        }
    }

    val api: JavspApi
        get() = retrofitApi ?: error("服务器地址未初始化")

    fun resolveUrl(path: String): String {
        if (path.startsWith("http://") || path.startsWith("https://")) return path
        val base = baseUrl.trimEnd('/')
        val suffix = if (path.startsWith("/")) path else "/$path"
        return base + suffix
    }

    companion object {
        fun normalizeBaseUrl(raw: String): String {
            var value = raw.trim().trimEnd('/')
            if (value.isEmpty()) throw IllegalArgumentException("请输入服务器地址")
            if (value.contains(' ') || value.contains('?') || value.contains('#')) {
                throw IllegalArgumentException("地址无效")
            }
            if (!value.startsWith("http://") && !value.startsWith("https://")) {
                value = "http://$value"
            }
            val scheme = value.substringBefore("://")
            if (scheme != "http" && scheme != "https") throw IllegalArgumentException("地址无效")
            val rest = value.substringAfter("://")
            if (rest.isBlank()) throw IllegalArgumentException("地址无效")
            return "$value/"
        }
    }
}
