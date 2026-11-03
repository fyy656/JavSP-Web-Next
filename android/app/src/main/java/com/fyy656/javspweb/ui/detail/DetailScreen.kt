package com.fyy656.javspweb.ui.detail

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Movie
import androidx.compose.material.icons.filled.CleaningServices
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.data.model.FilmUpdateRequest
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import com.fyy656.javspweb.data.model.Film

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DetailScreen(
    filmId: String,
    onBack: () -> Unit,
    onPlay: (filmId: String, title: String, startSeconds: Int, seriesId: String, season: Int, index: Int) -> Unit,
    viewModel: DetailViewModel = viewModel(factory = DetailViewModel.Factory),
) {
    val state by viewModel.state.collectAsState()
    val snackbar = remember { SnackbarHostState() }
    val context = LocalContext.current
    val app = context.applicationContext as JavspApp

    LaunchedEffect(filmId) { viewModel.load(filmId) }
    LaunchedEffect(state.film) { viewModel.ensurePollingIfActive() }
    LaunchedEffect(state.message) {
        if (state.message.isNotBlank()) {
            snackbar.showSnackbar(state.message)
            viewModel.consumeMessage()
        }
    }

    var confirmScrape by remember { mutableStateOf(false) }
    var editOpen by remember { mutableStateOf(false) }
    val isAdmin = app.isAdmin

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                title = { Text(state.film?.let { if (it.isSeries) "剧集" else "影片详情" } ?: "详情") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
            )
        },
    ) { padding ->
        if (state.loading) {
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            return@Scaffold
        }
        val film = state.film
        if (film == null) {
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                Text(state.error.ifBlank { "影片不存在" }, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            return@Scaffold
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(bottom = 24.dp),
        ) {
            item {
                FilmHeader(
                    film = film,
                    coverUrl = app.repository.coverUrl(film.id, film.updatedAt),
                    playLabel = playLabel(state),
                    playEnabled = !film.isSeries || state.episodes.isNotEmpty() || state.episodesLoading,
                    onPlay = {
                        if (film.isSeries) {
                            // 剧集条目本身没有视频文件，必须定位到具体分集
                            val resumeId = state.seriesProgress?.resume?.filmId.orEmpty()
                            val target = state.episodes.firstOrNull { it.id == resumeId }
                                ?: state.episodes.firstOrNull()
                            if (target != null) {
                                val start = target.progress.position.toInt().coerceAtLeast(0)
                                onPlay(
                                    target.id, target.displayTitle, start, film.id,
                                    target.season, state.episodes.indexOf(target),
                                )
                            } else {
                                viewModel.reportMessage("分集还没读取到，请稍后再试")
                            }
                        } else {
                            val start = film.progress.position.toInt().coerceAtLeast(0)
                            onPlay(film.id, film.displayTitle, start, "", 0, -1)
                        }
                    },
                    onScrape = { confirmScrape = true },
                    onEdit = if (isAdmin) { { editOpen = true } } else null,
                    canScrape = isAdmin,
                )
            }

            if (film.isSeries) {
                item {
                    SeriesSeasons(
                        seasons = state.seasons,
                        seasonFocus = state.seasonFocus,
                        watched = state.seriesProgress?.watched ?: 0,
                        total = state.seriesProgress?.total ?: 0,
                        onSelect = viewModel::selectSeason,
                    )
                }
                item {
                    if (state.episodesLoading) {
                        Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                            CircularProgressIndicator(Modifier.size(28.dp), strokeWidth = 2.dp)
                        }
                    }
                }
                items(state.episodes, key = { it.id }) { ep ->
                    val seriesId = film.id
                    val index = state.episodes.indexOf(ep)
                    EpisodeRow(episode = ep, onClick = {
                        val start = ep.progress.position.toInt().coerceAtLeast(0)
                        onPlay(ep.id, ep.displayTitle, start, seriesId, ep.season, index)
                    })
                }
            }
        }
    }

    val editingFilm = state.film
    if (editOpen && editingFilm != null) {
        EditFilmDialog(
            film = editingFilm,
            saving = state.saving,
            onDismiss = { editOpen = false },
            onSave = { body -> viewModel.save(body) { editOpen = false } },
        )
    }

    if (confirmScrape && state.film != null) {
        ScrapeDialog(
            film = state.film!!,
            initialId = state.dvdIdInput,
            onIdChange = viewModel::onDvdId,
            onDismiss = { confirmScrape = false },
            onConfirm = { override ->
                confirmScrape = false
                viewModel.scrapeCurrent(override)
            },
        )
    }
}

@Composable
private fun FilmHeader(
    film: Film,
    coverUrl: String,
    playLabel: String,
    playEnabled: Boolean,
    onPlay: () -> Unit,
    onScrape: () -> Unit,
    onEdit: (() -> Unit)? = null,
    canScrape: Boolean = true,
) {
    Column(Modifier.padding(16.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            AsyncImage(
                model = coverUrl,
                contentDescription = film.displayTitle,
                contentScale = ContentScale.Crop,
                placeholder = rememberVectorPainter(Icons.Filled.Movie),
                error = rememberVectorPainter(Icons.Filled.Movie),
                fallback = rememberVectorPainter(Icons.Filled.Movie),
                modifier = Modifier
                    .width(130.dp)
                    .aspectRatio(2f / 3f)
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant),
            )
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(film.displayTitle, fontWeight = FontWeight.Bold, fontSize = 18.sp, maxLines = 4)
                if (film.dvdid.isNotBlank()) {
                    InfoLine("ID", film.dvdid)
                }
                if (film.actress.isNotEmpty()) InfoLine("演员", film.actress.joinToString("、"))
                if (film.releaseDate.isNotBlank()) InfoLine("日期", film.releaseDate)
                if (film.studio.isNotBlank()) InfoLine("片商", film.studio)
                if (film.publisher.isNotBlank()) InfoLine("发行", film.publisher)
                if (film.director.isNotBlank()) InfoLine("导演", film.director)
                if (film.runtime > 0) InfoLine("时长", "${film.runtime} 分钟")
                StatusLine(film)
            }
        }

        Spacer(Modifier.height(14.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(
                onClick = onPlay,
                enabled = playEnabled,
                modifier = Modifier.weight(1f).height(46.dp),
            ) {
                Icon(Icons.Filled.PlayArrow, null)
                Spacer(Modifier.width(6.dp))
                Text(playLabel)
            }
            if (onEdit != null) {
                OutlinedButton(onClick = onEdit, modifier = Modifier.height(46.dp)) {
                    Icon(Icons.Filled.Edit, null)
                    Spacer(Modifier.width(6.dp))
                    Text("编辑")
                }
            }
            OutlinedButton(
                onClick = onScrape,
                enabled = canScrape && !film.isSeries && !film.isScraping,
                modifier = Modifier.height(46.dp),
            ) {
                if (film.isScraping) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                } else {
                    Icon(Icons.Filled.CleaningServices, null)
                }
                Spacer(Modifier.width(6.dp))
                Text(if (film.isSeries) "剧集不联网刮削" else "刮削")
            }
        }

        if (film.plot.isNotBlank()) {
            Spacer(Modifier.height(14.dp))
            Text(film.plot, fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (film.isScraping) {
            Spacer(Modifier.height(10.dp))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
                Text("正在刮削…", fontSize = 12.sp, color = MaterialTheme.colorScheme.primary)
            }
        }
        if (film.scrapeState == "failed" && film.lastError.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            Text(film.lastError, fontSize = 12.sp, color = MaterialTheme.colorScheme.error)
        }
    }
}

@Composable
private fun InfoLine(label: String, value: String) {
    Row {
        Text("$label：", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(
            value,
            fontSize = 12.sp,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun StatusLine(film: Film) {
    val text = when {
        film.isScraping -> "刮削中"
        film.scrapeState == "failed" -> "刮削失败"
        film.status == "missing" -> "文件丢失"
        film.status == "unscraped" -> "未刮削"
        film.status == "partial" -> "资料不完整（缺 ${film.missingFields.size} 项）"
        film.status == "complete" -> "资料完整"
        else -> film.status
    }
    Text(text, fontSize = 12.sp, color = MaterialTheme.colorScheme.primary)
}

@Composable
private fun SeriesSeasons(
    seasons: List<com.fyy656.javspweb.data.model.Season>,
    seasonFocus: Int,
    watched: Int,
    total: Int,
    onSelect: (Int) -> Unit,
) {
    Column(Modifier.padding(horizontal = 16.dp)) {
        if (total > 0) {
            Text("已看 $watched/$total 集", fontSize = 13.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(8.dp))
        }
        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(seasons, key = { it.season }) { s ->
                FilterChip(
                    selected = s.season == seasonFocus,
                    onClick = { onSelect(s.season) },
                    label = { Text(if (s.season == 0) "特别篇" else "第 ${s.season} 季") },
                )
            }
        }
        Spacer(Modifier.height(8.dp))
    }
}

@Composable
private fun EpisodeRow(episode: Film, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Surface(
            color = MaterialTheme.colorScheme.surfaceVariant,
            shape = RoundedCornerShape(8.dp),
            modifier = Modifier.size(40.dp),
        ) {
            Box(contentAlignment = Alignment.Center) {
                Text(
                    if (episode.episode > 0) "${episode.episode}" else "SP",
                    fontWeight = FontWeight.Bold,
                    fontSize = 15.sp,
                )
            }
        }
        Column(Modifier.weight(1f)) {
            Text(
                episode.title.ifBlank { "第 ${episode.episode} 集" },
                fontSize = 15.sp,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            if (!episode.progress.finished && episode.progress.percent > 0) {
                Spacer(Modifier.height(4.dp))
                LinearProgressIndicator(
                    progress = { (episode.progress.percent / 100f).toFloat() },
                    modifier = Modifier.fillMaxWidth(0.7f).height(3.dp),
                )
            }
        }
        if (episode.progress.finished) {
            Icon(
                Icons.Filled.Check,
                contentDescription = "已看",
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(20.dp),
            )
        } else {
            Icon(Icons.Filled.PlayArrow, contentDescription = "播放", tint = MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
private fun ScrapeDialog(
    film: Film,
    initialId: String,
    onIdChange: (String) -> Unit,
    onDismiss: () -> Unit,
    onConfirm: (String?) -> Unit,
) {
    var id by remember(film.id) { mutableStateOf(initialId.ifBlank { film.dvdid }) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("刮削这部影片") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("将以此番号联网刮削并写回 NFO，原有字段会保留。", fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedTextField(
                    value = id,
                    onValueChange = { id = it; onIdChange(it) },
                    label = { Text("番号（ID）") },
                    singleLine = true,
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(id.trim().ifBlank { null }) },
                enabled = id.isNotBlank(),
            ) { Text("开始刮削") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

/** 与网页端一致：主按钮只写「播放」，看到哪一集交给分集上的角标表示。 */
private fun playLabel(state: DetailUiState): String = "播放"

/** 资料编辑：电影 / 剧集 / 分集字段不同，保存后由服务端写入对应根节点的 NFO。 */
@Composable
private fun EditFilmDialog(
    film: Film,
    saving: Boolean,
    onDismiss: () -> Unit,
    onSave: (FilmUpdateRequest) -> Unit,
) {
    var dvdid by remember { mutableStateOf(film.dvdid) }
    var title by remember { mutableStateOf(film.title) }
    var originalTitle by remember { mutableStateOf(film.originalTitle) }
    var actress by remember { mutableStateOf(film.actress.joinToString("、")) }
    var releaseDate by remember { mutableStateOf(film.releaseDate) }
    var studio by remember { mutableStateOf(film.studio) }
    var publisher by remember { mutableStateOf(film.publisher) }
    var director by remember { mutableStateOf(film.director) }
    var runtime by remember { mutableStateOf(if (film.runtime > 0) film.runtime.toString() else "") }
    var plot by remember { mutableStateOf(film.plot) }

    val isMovie = !film.isSeries && !film.isEpisode
    val heading = when {
        film.isSeries -> "编辑剧集资料"
        film.isEpisode -> "编辑分集资料"
        else -> "编辑影片资料"
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(heading) },
        text = {
            Column(
                Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (isMovie) {
                    EditField("番号", dvdid) { dvdid = it }
                }
                EditField(if (film.isSeries) "剧名" else "标题", title) { title = it }
                EditField("原始标题", originalTitle) { originalTitle = it }
                EditField("演员（顿号分隔）", actress) { actress = it }
                EditField(if (film.isSeries) "首播日期" else "发行日期", releaseDate) { releaseDate = it }
                EditField(if (film.isSeries) "电视台 / 制作" else "片商", studio) { studio = it }
                if (isMovie) {
                    EditField("发行商", publisher) { publisher = it }
                    EditField("导演", director) { director = it }
                }
                EditField("时长（分钟）", runtime) { runtime = it }
                EditField(if (film.isSeries) "剧情简介" else "本集简介", plot, singleLine = false) { plot = it }
                Text(
                    when {
                        film.isSeries -> "保存后写入剧集目录的 tvshow.nfo"
                        film.isEpisode -> "保存后写入该集旁边的 NFO（episodedetails）"
                        else -> "保存后写入该影片的 NFO（movie）"
                    },
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = !saving,
                onClick = {
                    onSave(
                        FilmUpdateRequest(
                            dvdid = if (isMovie) dvdid.trim() else "",
                            title = title.trim(),
                            originalTitle = originalTitle.trim(),
                            plot = plot.trim(),
                            releaseDate = releaseDate.trim(),
                            studio = studio.trim(),
                            publisher = if (isMovie) publisher.trim() else "",
                            director = if (isMovie) director.trim() else "",
                            runtime = runtime.trim().toIntOrNull() ?: 0,
                            actress = actress.split('、', ',', '，', ';', '；')
                                .map { it.trim() }.filter { it.isNotEmpty() },
                        )
                    )
                },
            ) { Text(if (saving) "保存中…" else "保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun EditField(label: String, value: String, singleLine: Boolean = true, onChange: (String) -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label) },
        singleLine = singleLine,
        minLines = if (singleLine) 1 else 3,
        modifier = Modifier.fillMaxWidth(),
    )
}
