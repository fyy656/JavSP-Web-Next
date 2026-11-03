package com.fyy656.javspweb.ui.login

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.data.api.ApiClient
import com.fyy656.javspweb.ui.common.appViewModelFactory
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import androidx.lifecycle.viewmodel.initializer

data class LoginUiState(
    val server: String = "",
    val username: String = "admin",
    val password: String = "",
    val loading: Boolean = false,
    val error: String = "",
)

class LoginViewModel(private val app: JavspApp) : ViewModel() {
    private val _state = MutableStateFlow(LoginUiState())
    val state: StateFlow<LoginUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            _state.update { it.copy(server = app.settings.getServerUrl()) }
        }
    }

    fun onServer(v: String) = _state.update { it.copy(server = v, error = "") }
    fun onUsername(v: String) = _state.update { it.copy(username = v, error = "") }
    fun onPassword(v: String) = _state.update { it.copy(password = v, error = "") }

    fun login(onSuccess: () -> Unit) {
        val s = _state.value
        if (s.loading) return
        val baseUrl = try {
            ApiClient.normalizeBaseUrl(s.server)
        } catch (e: IllegalArgumentException) {
            _state.update { it.copy(error = e.message ?: "地址无效") }
            return
        }
        if (s.username.isBlank()) {
            _state.update { it.copy(error = "请输入用户名") }
            return
        }
        _state.update { it.copy(loading = true, error = "") }
        viewModelScope.launch {
            try {
                app.configureServer(baseUrl)
                val me = app.repository.login(s.username.trim(), s.password)
                app.setRole(me.role)
                app.settings.setServerUrl(baseUrl.trimEnd('/'))
                _state.update { it.copy(loading = false) }
                onSuccess()
            } catch (e: Exception) {
                _state.update { it.copy(loading = false, error = e.message ?: "登录失败") }
            }
        }
    }

    companion object {
        val Factory = appViewModelFactory { app: JavspApp -> LoginViewModel(app) }
    }
}
