package com.fyy656.javspweb

import android.app.Application
import coil.ImageLoader
import coil.ImageLoaderFactory
import com.fyy656.javspweb.data.JavspRepository
import com.fyy656.javspweb.data.SettingsStore
import com.fyy656.javspweb.data.api.ApiClient
import com.fyy656.javspweb.data.api.SessionCookieJar
import com.fyy656.javspweb.ui.common.coilImageLoader

class JavspApp : Application(), ImageLoaderFactory {
    lateinit var settings: SettingsStore
        private set

    val cookieJar: SessionCookieJar = SessionCookieJar()
    val apiClient: ApiClient = ApiClient(cookieJar)
    lateinit var repository: JavspRepository
        private set

    @Volatile var serverConfigured: Boolean = false
        private set

    /** 当前登录用户的角色（admin / operator），用于隐藏改资料的入口。 */
    @Volatile var role: String = ""
        private set

    val isAdmin: Boolean get() = role == "admin"

    fun setRole(value: String) {
        role = value
    }

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        repository = JavspRepository(apiClient)
    }

    /** 应用启动或更换服务器时调用，配置 Retrofit 基础地址。 */
    fun configureServer(baseUrl: String) {
        apiClient.setBaseUrl(baseUrl)
        serverConfigured = true
    }

    fun markServerCleared() {
        serverConfigured = false
        role = ""
    }

    /** Coil 单例：复用共享 OkHttpClient，封面自动带会话 Cookie。 */
    override fun newImageLoader(): ImageLoader = coilImageLoader(this, apiClient.httpClient)
}
