package com.fyy656.javspweb.data.api

import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl

/** 按 host 保存 Cookie：客户端只连接一台服务器，直连局域网 IP 时最稳妥。 */
class SessionCookieJar : CookieJar {
    private val store = mutableMapOf<String, MutableList<Cookie>>()

    @Synchronized
    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        val list = store.getOrPut(url.host) { mutableListOf() }
        for (cookie in cookies) {
            list.removeAll { it.name == cookie.name && it.path == cookie.path }
            if (cookie.expiresAt >= System.currentTimeMillis()) list.add(cookie)
        }
    }

    @Synchronized
    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        val now = System.currentTimeMillis()
        return store[url.host]
            ?.filter { it.expiresAt >= now && it.matches(url) }
            ?: emptyList()
    }

    @Synchronized
    fun clear() = store.clear()

    @Synchronized
    fun hasSession(host: String): Boolean =
        store[host]?.any { it.name == "javsp_web_session" } == true
}
