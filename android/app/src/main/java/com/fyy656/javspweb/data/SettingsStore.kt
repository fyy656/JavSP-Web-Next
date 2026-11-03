package com.fyy656.javspweb.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "javsp_settings")

class SettingsStore(private val context: Context) {
    private val serverKey = stringPreferencesKey("server_url")

    val serverUrl: Flow<String> = context.dataStore.data.map { it[serverKey] ?: "" }

    suspend fun getServerUrl(): String = serverUrl.first()

    suspend fun setServerUrl(value: String) {
        context.dataStore.edit { it[serverKey] = value }
    }
}
