package com.fyy656.javspweb.data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import retrofit2.Response
import java.io.IOException

/** 把 HTTP/网络错误统一转成中文异常，detail 直接用服务端返回的提示。 */
class ApiException(val code: Int, message: String) : IOException(message)

suspend fun <T : Any> apiCall(block: suspend () -> Response<T>): T {
    val response: Response<T> = try {
        block()
    } catch (e: Exception) {
        throw when (e) {
            is ApiException -> e
            else -> IOException("无法连接服务器：${e.message ?: "网络错误"}", e)
        }
    }
    if (response.isSuccessful) {
        @Suppress("UNCHECKED_CAST")
        return response.body() ?: (Unit as T)
    }
    val detail = parseErrorDetail(response.errorBody()?.string())
        ?: if (response.code() == 401) "未登录或登录已过期" else "HTTP ${response.code()}"
    throw ApiException(response.code(), detail)
}

private val errorJson = Json { ignoreUnknownKeys = true }

private fun parseErrorDetail(raw: String?): String? {
    if (raw.isNullOrBlank()) return null
    return try {
        val obj = errorJson.parseToJsonElement(raw).jsonObject
        obj["detail"]?.jsonPrimitive?.content
            ?: obj["message"]?.jsonPrimitive?.content
            ?: raw.take(200)
    } catch (_: Exception) {
        raw.take(200)
    }
}
