package com.fyy656.javspweb.ui.player

import android.content.pm.ActivityInfo
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AspectRatio
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.ScreenRotation
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.hls.HlsMediaSource
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import com.fyy656.javspweb.JavspApp

/** 画面比例档位：与服务端无关，仅影响本地渲染。 */
private enum class AspectMode(
    val label: String,
    val resizeMode: Int,
    val ratio: Float,
) {
    FIT("适应屏幕", AspectRatioFrameLayout.RESIZE_MODE_FIT, 0f),
    FILL("拉伸铺满", AspectRatioFrameLayout.RESIZE_MODE_FILL, 0f),
    ZOOM("裁剪填充", AspectRatioFrameLayout.RESIZE_MODE_ZOOM, 0f),
    R16_9("强制 16:9", AspectRatioFrameLayout.RESIZE_MODE_FIT, 16f / 9f),
    R4_3("强制 4:3", AspectRatioFrameLayout.RESIZE_MODE_FIT, 4f / 3f),
}

private val SPEEDS = listOf(0.5f, 0.75f, 1f, 1.25f, 1.5f, 2f)

/** 屏幕方向：自动按视频比例（竖屏资源不再被强行横过来），也可手动锁定。 */
private enum class OrientationMode(val label: String) {
    AUTO("自动旋转"),
    LANDSCAPE("锁定横屏"),
    PORTRAIT("锁定竖屏"),
}

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
@Composable
fun PlayerScreen(
    filmId: String,
    startSeconds: Int,
    seriesId: String,
    season: Int,
    currentIndex: Int,
    onExit: () -> Unit,
    viewModel: PlayerViewModel = viewModel(factory = PlayerViewModel.Factory),
) {
    val context = LocalContext.current
    val app = context.applicationContext as JavspApp
    val state by viewModel.state.collectAsState()

    val activity = context as? android.app.Activity
    val view = LocalView.current
    var orientationMode by remember { mutableStateOf(OrientationMode.AUTO) }
    var videoIsPortrait by remember { mutableStateOf(false) }

    // 播放期间不锁屏；进入播放器时隐藏系统栏（否则底部会留一条导航栏白边）
    DisposableEffect(Unit) {
        val original = activity?.requestedOrientation
        activity?.window?.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val controller = activity?.window?.let { WindowInsetsControllerCompat(it, view) }
        controller?.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        controller?.hide(WindowInsetsCompat.Type.systemBars())
        onDispose {
            controller?.show(WindowInsetsCompat.Type.systemBars())
            activity?.requestedOrientation = original ?: ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
            activity?.window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    // 自动模式跟随视频比例：竖屏资源就竖着放，不再一律横过来
    LaunchedEffect(orientationMode, videoIsPortrait) {
        if (activity == null) return@LaunchedEffect
        activity.requestedOrientation = when (orientationMode) {
            OrientationMode.LANDSCAPE -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            OrientationMode.PORTRAIT -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
            OrientationMode.AUTO -> if (videoIsPortrait) {
                ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
            } else {
                ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            }
        }
    }

    // 共享 OkHttp（带会话 Cookie），本地 /stream 才能鉴权
    val httpFactory = remember {
        OkHttpDataSource.Factory(app.apiClient.httpClient).setUserAgent("JavSPAndroid/2.0")
    }

    val exoPlayer = remember {
        ExoPlayer.Builder(context)
            .setMediaSourceFactory(DefaultMediaSourceFactory(context).setDataSourceFactory(httpFactory))
            .build()
    }

    // 普通持有者（不是 Compose 状态）：避免在 AndroidView 工厂（组合期）写状态
    val playerViewHolder = remember { arrayOfNulls<PlayerView>(1) }
    var controlsVisible by remember { mutableStateOf(true) }
    var aspectMode by remember { mutableStateOf(AspectMode.FIT) }
    var speed by remember { mutableStateOf(1f) }
    var ratioMenuOpen by remember { mutableStateOf(false) }
    var speedMenuOpen by remember { mutableStateOf(false) }

    val currentPlayback by rememberUpdatedState(state.playback)
    var playbackError by remember { mutableStateOf<String?>(null) }
    var fallbackUsed by remember { mutableStateOf(false) }

    DisposableEffect(exoPlayer) {
        val listener = object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                if (playbackState == Player.STATE_READY) {
                    playbackError = null
                    viewModel.setDuration(exoPlayer.duration.coerceAtLeast(0L))
                    val size = exoPlayer.videoSize
                    if (size.width > 0 && size.height > 0) {
                        videoIsPortrait = size.height > size.width
                    }
                }
            }

            override fun onPlayerError(error: PlaybackException) {
                val pb = currentPlayback
                if (pb != null && pb.isLocal && pb.fallbackMode == "emby" && !fallbackUsed) {
                    // 本地直连/直封装失败一次：自动改走媒体服务器转码
                    fallbackUsed = true
                    viewModel.fallbackToTranscode()
                } else {
                    playbackError = error.localizedMessage ?: "播放失败"
                }
            }
        }
        exoPlayer.addListener(listener)
        onDispose { exoPlayer.removeListener(listener) }
    }

    viewModel.bindPositionProvider { exoPlayer.currentPosition }

    // 首次进入播放页要主动拉一次播放地址；之前只在「重试」里调用，导致一直停在加载中
    LaunchedEffect(filmId, startSeconds, seriesId, season, currentIndex) {
        viewModel.prepare(filmId, "", startSeconds, seriesId, season, currentIndex)
    }

    // 播放地址变化时装载
    LaunchedEffect(state.resolvedUrl, state.isHls, state.startMs) {
        val url = state.resolvedUrl
        if (url.isBlank()) return@LaunchedEffect
        playbackError = null
        if (state.playback?.isLocal == true) fallbackUsed = false
        val mediaItem = MediaItem.Builder().setUri(url).build()
        val source = if (state.isHls) {
            HlsMediaSource.Factory(httpFactory).createMediaSource(mediaItem)
        } else {
            ProgressiveMediaSource.Factory(httpFactory).createMediaSource(mediaItem)
        }
        exoPlayer.setMediaSource(source)
        exoPlayer.prepare()
        if (state.startMs > 0) exoPlayer.seekTo(state.startMs)
        exoPlayer.setPlaybackSpeed(speed)
        exoPlayer.playWhenReady = true
    }

    // 切换比例后立刻应用；只靠 AndroidView 的 update 回调有时不会触发，导致「回不去」
    LaunchedEffect(aspectMode) { playerViewHolder[0]?.let { applyAspect(it, aspectMode) } }

    LaunchedEffect(speed) { runCatching { exoPlayer.setPlaybackSpeed(speed) } }

    DisposableEffect(Unit) {
        onDispose {
            viewModel.stopPlayback(reportProgress = true)
            exoPlayer.release()
        }
    }

    BackHandler(enabled = true) { onExit() }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(
            factory = { ctx ->
                PlayerView(ctx).apply {
                    player = exoPlayer
                    useController = true
                    controllerShowTimeoutMs = 3500
                    controllerAutoShow = true
                    setShowNextButton(false)
                    setShowPreviousButton(false)
                    setControllerVisibilityListener(
                        PlayerView.ControllerVisibilityListener { visibility ->
                            // 系统控制条隐藏时，同步收起自定义顶栏
                            controlsVisible = visibility == android.view.View.VISIBLE
                        }
                    )
                }.also { playerViewHolder[0] = it }
            },
            modifier = Modifier.fillMaxSize(),
            update = { view ->
                playerViewHolder[0] = view
                applyAspect(view, aspectMode)
            },
        )

        // ---------------------------------------------------------- 顶部信息栏（随控制条隐藏）
        AnimatedVisibility(
            visible = controlsVisible,
            enter = fadeIn() + slideInVertically { -it },
            exit = fadeOut() + slideOutVertically { -it },
            modifier = Modifier.align(Alignment.TopCenter),
        ) {
            Row(
                Modifier
                    .fillMaxWidth()
                    .background(Color.Black.copy(alpha = 0.45f))
                    .statusBarsPadding()
                    .padding(horizontal = 4.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = onExit) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, "返回", tint = Color.White)
                }
                Text(
                    state.title.ifBlank { "播放" },
                    color = Color.White,
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f).padding(horizontal = 6.dp),
                )
                IconButton(onClick = {
                    orientationMode = when (orientationMode) {
                        OrientationMode.AUTO -> OrientationMode.LANDSCAPE
                        OrientationMode.LANDSCAPE -> OrientationMode.PORTRAIT
                        OrientationMode.PORTRAIT -> OrientationMode.AUTO
                    }
                }) {
                    Icon(Icons.Filled.ScreenRotation, orientationMode.label, tint = Color.White)
                }
                if (state.playback != null) {
                    // 与网页端一致：按钮显示要切换到的目标模式
                    TextButton(onClick = { viewModel.switchPlaybackMode() }) {
                        Text(
                            if (state.playback?.isLocal == true) "转码" else "直连",
                            color = Color.White,
                            style = MaterialTheme.typography.labelLarge,
                        )
                    }
                }
                if (state.episodes.isNotEmpty()) {
                    IconButton(onClick = { viewModel.playAdjacent(-1) }, enabled = state.hasPrev) {
                        Icon(Icons.Filled.SkipPrevious, "上一集",
                            tint = if (state.hasPrev) Color.White else Color.Gray)
                    }
                    IconButton(onClick = { viewModel.playAdjacent(1) }, enabled = state.hasNext) {
                        Icon(Icons.Filled.SkipNext, "下一集",
                            tint = if (state.hasNext) Color.White else Color.Gray)
                    }
                }
            }
        }

        // ---------------------------------------------------------- 右侧工具（比例 / 倍速）
        AnimatedVisibility(
            visible = controlsVisible && state.error.isBlank() && playbackError == null,
            enter = fadeIn(),
            exit = fadeOut(),
            modifier = Modifier.align(Alignment.BottomEnd).padding(bottom = 64.dp, end = 10.dp),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Box {
                    ToolButton(icon = Icons.Filled.AspectRatio, label = aspectMode.label) {
                        ratioMenuOpen = true
                    }
                    DropdownMenu(expanded = ratioMenuOpen, onDismissRequest = { ratioMenuOpen = false }) {
                        AspectMode.entries.forEach { mode ->
                            DropdownMenuItem(
                                text = { Text(mode.label) },
                                onClick = { aspectMode = mode; ratioMenuOpen = false },
                                trailingIcon = {
                                    if (mode == aspectMode) Icon(Icons.Filled.Check, null)
                                },
                            )
                        }
                    }
                }
                Box {
                    ToolButton(icon = Icons.Filled.Speed, label = speedLabel(speed)) {
                        speedMenuOpen = true
                    }
                    DropdownMenu(expanded = speedMenuOpen, onDismissRequest = { speedMenuOpen = false }) {
                        SPEEDS.forEach { value ->
                            DropdownMenuItem(
                                text = { Text(speedLabel(value)) },
                                onClick = { speed = value; speedMenuOpen = false },
                                trailingIcon = {
                                    if (value == speed) Icon(Icons.Filled.Check, null)
                                },
                            )
                        }
                    }
                }
            }
        }

        when {
            state.loading -> CircularProgressIndicator(
                color = Color.White,
                modifier = Modifier.align(Alignment.Center),
            )
            state.error.isNotBlank() -> ErrorPanel(
                message = state.error,
                onRetry = { viewModel.prepare(filmId, "", startSeconds, seriesId, season, currentIndex) },
                onBack = onExit,
            )
            playbackError != null -> ErrorPanel(
                message = playbackError!!,
                onRetry = {
                    playbackError = null
                    if (state.playback?.isLocal == true) {
                        fallbackUsed = true
                        viewModel.fallbackToTranscode()
                    } else {
                        viewModel.prepare(filmId, "", startSeconds, seriesId, season, currentIndex)
                    }
                },
                onBack = onExit,
            )
        }
    }
}

/** 应用画面比例：拉伸/裁剪改 resizeMode，强制比例改内部 content frame。 */
private fun applyAspect(view: PlayerView, mode: AspectMode) {
    view.resizeMode = mode.resizeMode
    runCatching {
        view.findViewById<AspectRatioFrameLayout>(androidx.media3.ui.R.id.exo_content_frame)
            ?.setAspectRatio(mode.ratio)
    }
    view.requestLayout()
}

@Composable
private fun ToolButton(    icon: androidx.compose.ui.graphics.vector.ImageVector,
    label: String,
    onClick: () -> Unit,
) {
    Row(
        Modifier
            .clip(RoundedCornerShape(50))
            .background(Color.Black.copy(alpha = 0.55f))
            .clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Icon(icon, contentDescription = null, tint = Color.White, modifier = Modifier.size(16.dp))
        Text(label, color = Color.White, style = MaterialTheme.typography.labelMedium)
    }
}

private fun speedLabel(value: Float): String =
    if (value == 1f) "倍速 1.0x" else "倍速 " + (if (value % 1f == 0f) "${value.toInt()}.0x" else "${value}x")

@Composable
private fun ErrorPanel(message: String, onRetry: () -> Unit, onBack: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("无法播放", color = Color.White, style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(8.dp))
        Text(message, color = Color.LightGray)
        Spacer(Modifier.height(16.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            OutlinedButton(onClick = onBack) { Text("返回") }
            Button(onClick = onRetry) { Text("重试") }
        }
    }
}
