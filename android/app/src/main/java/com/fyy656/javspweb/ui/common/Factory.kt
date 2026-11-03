package com.fyy656.javspweb.ui.common

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.CreationExtras
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fyy656.javspweb.JavspApp

/** 从 CreationExtras 取出 JavspApp，用于手动构造带仓库依赖的 ViewModel。 */
inline fun <reified VM : ViewModel> appViewModelFactory(
    crossinline create: (JavspApp) -> VM,
): ViewModelProvider.Factory = viewModelFactory {
    initializer {
        val app = (this[ViewModelProvider.AndroidViewModelFactory.APPLICATION_KEY] as JavspApp)
        create(app)
    }
}
