package com.fyy656.javspweb.ui.library

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.items as gridItems
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Sort
import androidx.compose.material.icons.filled.Category
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CleaningServices
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import coil.request.ImageRequest
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.data.model.CoverRef
import com.fyy656.javspweb.data.model.Film
import com.fyy656.javspweb.data.model.GroupItem
import com.fyy656.javspweb.data.model.Library
import kotlinx.coroutines.flow.distinctUntilChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.foundation.ExperimentalFoundationApi

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun LibraryScreen(
    onOpenFilm: (String) -> Unit,
    onLoggedOut: () -> Unit,
    viewModel: LibraryViewModel = viewModel(factory = LibraryViewModel.Factory),
) {
    val state by viewModel.state.collectAsState()
    val snackbar = remember { SnackbarHostState() }
    val context = LocalContext.current
    val app = context.applicationContext as JavspApp

    LaunchedEffect(state.message) {
        if (state.message.isNotBlank()) {
            snackbar.showSnackbar(state.message)
            viewModel.consumeMessage()
        }
    }
    LaunchedEffect(state.error) {
        if (state.error.isNotBlank()) {
            snackbar.showSnackbar(state.error)
            viewModel.consumeError()
        }
    }
    LaunchedEffect(state.loggedOut) {
        if (state.loggedOut) onLoggedOut()
    }

    var confirmBatch by remember { mutableStateOf(false) }
    var menuOpen by remember { mutableStateOf(false) }
    var sortOpen by remember { mutableStateOf(false) }
    var groupOpen by remember { mutableStateOf(false) }

    // 返回键：先退出某个分类，再退出分类浏览，最后才离开页面
    BackHandler(enabled = state.groupKey != null) { viewModel.backToGroups() }
    BackHandler(enabled = state.groupBy.isNotBlank() && state.groupKey == null) { viewModel.setGroupBy("") }
    // 剧集库没有联网刮削，批量刮削按钮与网页端一样隐藏
    val isSeriesLibrary = state.libraries.firstOrNull { it.id == state.selectedLibraryId }?.kind == "series"

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                title = { Text("媒体库") },
                actions = {
                    IconButton(onClick = { viewModel.refresh() }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "刷新")
                    }
                    if (!isSeriesLibrary) {
                        Box {
                            IconButton(onClick = { groupOpen = true }) {
                                Icon(Icons.Filled.Category, contentDescription = "分类")
                            }
                            DropdownMenu(expanded = groupOpen, onDismissRequest = { groupOpen = false }) {
                                GROUPS.forEach { (key, label) ->
                                    DropdownMenuItem(
                                        text = { Text(label) },
                                        onClick = { groupOpen = false; viewModel.setGroupBy(key) },
                                        trailingIcon = {
                                            if (key == state.groupBy) Icon(Icons.Filled.Check, null)
                                        },
                                    )
                                }
                            }
                        }
                    }
                    Box {
                        IconButton(onClick = { sortOpen = true }) {
                            Icon(Icons.AutoMirrored.Filled.Sort, contentDescription = "排序")
                        }
                        DropdownMenu(expanded = sortOpen, onDismissRequest = { sortOpen = false }) {
                            SORTS.forEach { (key, label) ->
                                DropdownMenuItem(
                                    text = { Text(label) },
                                    onClick = { sortOpen = false; viewModel.setSort(key) },
                                    trailingIcon = {
                                        if (key == state.sort) Icon(Icons.Filled.Check, null)
                                    },
                                )
                            }
                        }
                    }
                    Box {
                        IconButton(onClick = { menuOpen = true }) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "更多")
                        }
                        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                            DropdownMenuItem(
                                text = { Text("扫描当前媒体库") },
                                onClick = { menuOpen = false; viewModel.startScan() },
                                leadingIcon = { Icon(Icons.Filled.Sync, null) },
                            )
                            // 剧集库不联网刮削；非管理员也不能刮削（服务端会 403）
                            if (!isSeriesLibrary && state.isAdmin) {
                                DropdownMenuItem(
                                    text = { Text("批量刮削当前筛选") },
                                    onClick = { menuOpen = false; confirmBatch = true },
                                    leadingIcon = { Icon(Icons.Filled.CleaningServices, null) },
                                )
                            }
                            HorizontalDivider()
                            DropdownMenuItem(
                                text = { Text("退出登录") },
                                onClick = { menuOpen = false; viewModel.logout(onLoggedOut) },
                            )
                        }
                    }
                },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            LibraryTabs(
                libraries = state.libraries,
                selectedId = state.selectedLibraryId,
                onSelect = viewModel::selectLibrary,
            )

            SearchAndFilter(
                query = state.query,
                status = state.statusFilter,
                onQuery = viewModel::setQuery,
                onSubmit = viewModel::submitQuery,
                onStatus = viewModel::setStatusFilter,
                scanning = state.scanning,
                scanDiscovered = state.scan?.discovered ?: 0,
            )

            if (state.groupBy.isNotBlank() && state.groupKey == null) {
                GroupList(
                    groups = state.groups,
                    loading = state.groupsLoading,
                    coverFor = { cover -> app.repository.coverUrl(cover.id, cover.v) },
                    onOpen = viewModel::openGroup,
                    onExit = { viewModel.setGroupBy("") },
                )
            } else {
                if (state.groupKey != null) {
                    GroupBackBar(
                        label = groupLabel(state.groupBy),
                        name = state.groups.firstOrNull { it.key == state.groupKey }?.name
                            ?: state.groupKey.orEmpty(),
                        onBack = viewModel::backToGroups,
                    )
                }
                Box(Modifier.fillMaxSize()) {
                    FilmGrid(
                        state = state,
                        coverFor = { film -> app.repository.coverUrl(film.id, film.updatedAt) },
                        onOpen = onOpenFilm,
                        onLoadMore = viewModel::loadNextPage,
                    )
                    if (state.loading) {
                        CircularProgressIndicator(Modifier.align(Alignment.Center))
                    }
                }
            }
        }
    }

    if (confirmBatch) {
        AlertDialog(
            onDismissRequest = { confirmBatch = false },
            title = { Text("批量刮削") },
            text = {
                Text(
                    "将把当前筛选范围内 ${state.total} 部影片加入刮削队列。" +
                        "已资料完整的也会入队用于补齐字段，原有 NFO 字段会保留。"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmBatch = false
                    viewModel.batchScrapeCurrent()
                }) { Text("开始刮削") }
            },
            dismissButton = { TextButton(onClick = { confirmBatch = false }) { Text("取消") } },
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun LibraryTabs(
    libraries: List<Library>,
    selectedId: String?,
    onSelect: (String?) -> Unit,
) {
    val listState = rememberLazyListState()
    LazyRow(
        state = listState,
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        // 「全部影片」= 跨库汇总；剧集由各自剧集库浏览（与服务端筛选规则一致）
        item(key = "__all__") {
            FilterChip(
                selected = selectedId == null,
                onClick = { onSelect(null) },
                label = { Text("全部影片") },
            )
        }
        items(libraries, key = { it.id }) { lib ->
            val selected = lib.id == selectedId
            FilterChip(
                selected = selected,
                onClick = { onSelect(lib.id) },
                label = { Text(lib.name) },
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SearchAndFilter(
    query: String,
    status: FilmStatusFilter,
    onQuery: (String) -> Unit,
    onSubmit: () -> Unit,
    onStatus: (FilmStatusFilter) -> Unit,
    scanning: Boolean,
    scanDiscovered: Int,
) {
    Column(Modifier.padding(horizontal = 12.dp)) {
        OutlinedTextField(
            value = query,
            onValueChange = onQuery,
            modifier = Modifier.fillMaxWidth(),
            placeholder = { Text("搜索番号 / 标题 / 演员 / 路径") },
            leadingIcon = { Icon(Icons.Filled.Search, null) },
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
        )
        if (scanning) {
            Row(
                Modifier.fillMaxWidth().padding(vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
                Text("正在扫描…已发现 $scanDiscovered 部", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        LazyRow(
            contentPadding = PaddingValues(vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(FilmStatusFilter.entries.toList()) { filter ->
                FilterChip(
                    selected = filter == status,
                    onClick = { onStatus(filter) },
                    label = { Text(filter.label) },
                )
            }
        }
    }
}

@Composable
private fun FilmGrid(
    state: LibraryUiState,
    coverFor: (Film) -> String,
    onOpen: (String) -> Unit,
    onLoadMore: () -> Unit,
) {
    val gridState = rememberLazyGridState()

    // 触底加载下一页
    LaunchedEffect(gridState, state.items.size) {
        snapshotFlow {
            val last = gridState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            last >= state.items.size - 8
        }.distinctUntilChanged().collect { nearEnd ->
            if (nearEnd && state.hasMore && !state.appendLoading) onLoadMore()
        }
    }

    if (state.items.isEmpty() && !state.loading) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("没有符合条件的影片", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        return
    }

    Box(Modifier.fillMaxSize()) {
        LazyVerticalGrid(
            columns = GridCells.Adaptive(minSize = 140.dp),
            state = gridState,
            contentPadding = PaddingValues(12.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            modifier = Modifier.fillMaxSize(),
        ) {
            gridItems(state.items, key = { it.id }) { film ->
                FilmCard(film = film, coverUrl = coverFor(film), onClick = { onOpen(film.id) })
            }
            if (state.appendLoading) {
                item(span = { GridItemSpan(maxLineSpan) }) {
                    Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(Modifier.size(28.dp), strokeWidth = 2.dp)
                    }
                }
            }
        }
    }
}

@Composable
private fun FilmCard(film: Film, coverUrl: String, onClick: () -> Unit) {
    Column(
        Modifier
            .clip(RoundedCornerShape(12.dp))
            .clickable(onClick = onClick),
    ) {
        Box(
            Modifier
                .fillMaxWidth()
                .aspectRatio(2f / 3f)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant),
        ) {
            AsyncImage(
                model = ImageRequest.Builder(LocalContext.current)
                    .data(coverUrl)
                    .crossfade(true)
                    .build(),
                contentDescription = film.displayTitle,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
            ScrapeBadge(film = film, modifier = Modifier.align(Alignment.TopStart).padding(6.dp))
            if (film.isSeries && (film.episodeCount > 0 || film.seasonCount > 0)) {
                Surface(
                    color = Color.Black.copy(alpha = 0.55f),
                    shape = RoundedCornerShape(6.dp),
                    modifier = Modifier.align(Alignment.BottomEnd).padding(6.dp),
                ) {
                    Text(
                        if (film.episodeCount > 0) "${film.episodeCount} 集" else "${film.seasonCount} 季",
                        color = Color.White,
                        fontSize = 11.sp,
                        modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                    )
                }
            }
            if (film.progress.percent > 0) {
                LinearProgressIndicator(
                    progress = { (film.progress.percent / 100f).toFloat() },
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .fillMaxWidth(),
                )
            }
        }
        Spacer(Modifier.height(4.dp))
        Text(
            film.displayTitle,
            fontSize = 13.sp,
            fontWeight = FontWeight.Medium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
        val sub = film.dvdid.ifBlank { film.releaseDate.take(4) }
        if (sub.isNotBlank()) {
            Text(sub, fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
        }
    }
}

@Composable
private fun ScrapeBadge(film: Film, modifier: Modifier = Modifier) {
    val pair: Pair<String, Color> = when {
        film.isScraping -> "刮削中" to MaterialTheme.colorScheme.primary
        film.scrapeState == "failed" -> "失败" to MaterialTheme.colorScheme.error
        film.status == "missing" -> "丢失" to MaterialTheme.colorScheme.error
        film.status == "unscraped" -> "未刮" to Color(0xFFD97706)
        film.status == "partial" -> "缺资料" to Color(0xFFD97706)
        else -> return
    }
    val text = pair.first
    val color = pair.second
    Surface(color = color.copy(alpha = 0.9f), shape = RoundedCornerShape(6.dp), modifier = modifier) {
        Text(text, color = Color.White, fontSize = 10.sp,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp))
    }
}

/** 排序项与网页端一致：最近更新 / 发行日期 / 番号 / 文件大小 */
private val SORTS = listOf(
    "updated" to "最近更新",
    "release" to "发行日期",
    "dvdid" to "番号",
    "size" to "文件大小",
)

/** 分类维度与网页端一致 */
private val GROUPS = listOf(
    "" to "不分类",
    "actress" to "按女优",
    "studio" to "按片商",
    "year" to "按年份",
)

private fun groupLabel(by: String): String = when (by) {
    "actress" -> "女优"
    "studio" -> "片商"
    "year" -> "年份"
    else -> "分类"
}

/** 分类内的返回条：与网页端一样做成醒目的按钮，而不是一行小字。 */
@Composable
private fun GroupBackBar(label: String, name: String, onBack: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        FilledTonalButton(onClick = onBack, contentPadding = PaddingValues(horizontal = 14.dp)) {
            Icon(Icons.AutoMirrored.Filled.ArrowBack, null, modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(6.dp))
            Text("返回${label}列表")
        }
        Text(
            "当前：$name",
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            fontSize = 13.sp,
        )
    }
}

@Composable
private fun GroupList(
    groups: List<GroupItem>,
    loading: Boolean,
    coverFor: (CoverRef) -> String,
    onOpen: (String) -> Unit,
    onExit: () -> Unit,
) {
    Box(Modifier.fillMaxSize()) {
        LazyVerticalGrid(
            columns = GridCells.Fixed(2),
            contentPadding = PaddingValues(12.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier.fillMaxSize(),
        ) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    TextButton(onClick = onExit) { Text("← 退出分类浏览") }
                    Text(
                        "共 ${groups.size} 个分类",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            gridItems(groups, key = { it.key.ifBlank { "__unknown__" } }) { group ->
                GroupCard(
                    group = group,
                    coverUrl = group.covers.firstOrNull()?.let(coverFor),
                    onClick = { onOpen(group.key) },
                )
            }
        }
        if (loading) CircularProgressIndicator(Modifier.align(Alignment.Center))
    }
}

@Composable
private fun GroupCard(group: GroupItem, coverUrl: String?, onClick: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column {
            Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f)) {
                if (coverUrl != null) {
                    AsyncImage(
                        model = coverUrl,
                        contentDescription = group.name,
                        contentScale = ContentScale.Crop,
                        modifier = Modifier.fillMaxSize(),
                    )
                } else {
                    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.surfaceVariant))
                }
            }
            Column(Modifier.padding(10.dp)) {
                Text(
                    group.name.ifBlank { "未知" },
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 14.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    "${group.count} 部",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
