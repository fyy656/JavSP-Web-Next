package com.fyy656.javspweb.ui.common

import coil.ImageLoader
import coil.disk.DiskCache
import coil.memory.MemoryCache

/** Coil 单例 ImageLoader 工厂由 [com.fyy656.javspweb.JavspApp] 实现。 */
fun coilImageLoader(context: android.content.Context, httpClient: okhttp3.OkHttpClient): ImageLoader =
    ImageLoader.Builder(context)
        .okHttpClient(httpClient)
        .crossfade(true)
        .memoryCache {
            MemoryCache.Builder(context).maxSizePercent(0.25).build()
        }
        .diskCache {
            DiskCache.Builder()
                .directory(context.cacheDir.resolve("image_cache"))
                .maxSizePercent(0.05)
                .build()
        }
        .respectCacheHeaders(false)
        .build()
