package com.fyy656.javspweb

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import com.fyy656.javspweb.data.api.ApiClient
import com.fyy656.javspweb.ui.AppNav
import com.fyy656.javspweb.ui.theme.JavspTheme
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val app = application as JavspApp
        lifecycleScope.launch {
            val saved = app.settings.getServerUrl()
            if (saved.isNotBlank()) {
                runCatching { app.configureServer(ApiClient.normalizeBaseUrl(saved)) }
            }
            setContent {
                JavspTheme {
                    val serverUrl by app.settings.serverUrl.collectAsState(initial = saved)
                    AppNav(
                        app = app,
                        hasServer = serverUrl.isNotBlank(),
                    )
                }
            }
        }
    }
}
