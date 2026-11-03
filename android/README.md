# JavSP for Android（原生客户端）

连接现有 **JavSP-Web 服务端** 的原生 Android 客户端，不是手机本地刮削器。服务端代码无需任何改动。

- 技术栈：Kotlin + Jetpack Compose + Retrofit/OkHttp + Coil + Media3 ExoPlayer，单 Activity + Navigation。
- 功能：媒体库浏览（成人 / 电影 / 剧集三类库）、搜索与状态筛选、排序（最近更新 / 发行日期 / 番号 / 文件大小）、分类浏览（女优 / 片商 / 年份）、后台扫描、单片与批量刮削、影片 / 剧集详情、内置播放器（本地直连 / 直封装 / HLS 转码、手动切换直连与转码、剧集上一集 / 下一集、跨设备播放进度）。
- 资料编辑：管理员可在详情页编辑影片 / 剧集 / 分集资料并写入对应 NFO（电影 movie、剧集 tvshow、分集 episodedetails）；非管理员看不到编辑与刮削入口。
- 不做：建库 / 文件夹管理、原始 NFO 编辑、季 NFO 编辑、AI、下载器、系统设置（这些在桌面端完成）。
- 刮削引擎仍运行在 PC / NAS 的 JavSP-Web 服务端，手机只负责操作与播放。

## 工作方式

1. 首次启动填写服务端地址（如 `http://192.168.1.10:8090`）与账号密码，调用 `POST /api/auth/login`，会话 Cookie 由 OkHttp CookieJar 保管。
2. 之后所有接口走 `/api/library/*`；封面 `/api/library/films/{id}/cover` 与本地流 `/stream` 都自动带会话 Cookie。
3. 播放：`GET /api/library/films/{id}/playback`
   - `mode=local`：相对 `/stream`，用共享 OkHttp DataSource 注入 Cookie，ExoPlayer 渐进式播放（服务端 Range/206）。
   - `mode=emby`：Emby/Jellyfin 直连绝对地址（`.m3u8` 走 HLS，其余渐进式），关闭 / 切集时上报 `playback/stop` 停转码。
   - 本地直连失败且 `fallback_mode=emby` 时自动回落转码。
4. 刮削：单片 `POST /films/{id}/scrape`、批量 `POST /films/scrape`，靠重新拉取 `films` 的 `scrape_state`（queued/running/idle/failed）轮询进度。
5. 扫描：`POST /libraries/{id}/scan`，轮询 `GET /scans/{id}`。

## 构建

要求：

- JDK 17+（本机使用 JDK 21）
- Android SDK Platform 35、Build-Tools 35.0.0
- Gradle 8.10.2（仓库已带 wrapper；若 wrapper 下载发行包超时，可用本机 `C:/Android/gradle-8.10.2`）

```powershell
cd android
.\gradlew.bat assembleDebug
# 正式签名包
.\gradlew.bat assembleRelease
```

APK 输出：

```text
android/app/build/outputs/apk/debug/app-debug.apk
android/app/build/outputs/apk/release/app-release.apk
```

`local.properties` 填写 SDK 路径：

```properties
sdk.dir=C:/Android/Sdk
```

## 签名

正式包使用 `android/keystore/javsp-release.jks`，密码在 `android/keystore.properties`，两者都被 `.gitignore` 排除。**请备份这两个文件**：覆盖安装必须使用同一签名。

包名沿用旧版 `com.fyy656.javspweb`（原生版 `versionCode=6 / versionName=1.0.0`），可直接覆盖安装旧 WebView 版；无 keystore 时 `assembleRelease` 生成未签名包，日常调试用 `assembleDebug`（applicationId 后缀 `.debug`，与正式版共存）。

## 目录结构

```text
app/src/main/java/com/fyy656/javspweb/
  JavspApp.kt              Application：容器、Coil 单例
  MainActivity.kt          单 Activity，启动时恢复服务器地址
  data/
    model/Models.kt        服务端 JSON DTO（字段与服务端逐字对齐）
    api/JavspApi.kt        Retrofit 接口（/api/library/*）
    api/ApiClient.kt       OkHttp + Retrofit，baseUrl 可变
    api/SessionCookieJar.kt 按 host 保存会话 Cookie
    JavspRepository.kt     数据仓库 + URL 拼装
    SettingsStore.kt       DataStore：保存服务器地址
  ui/
    login/                 服务器 + 账号登录
    library/               媒体库海报墙、搜索筛选、扫描、批量刮削
    detail/                影片 / 剧集详情、季切换、分集、单片刮削
    player/                Media3 播放器、上下集、进度、停流
    theme/                 颜色与 Material3 主题
```

## 限制

- 必须能访问 JavSP-Web 服务端；没有服务端时不能刮削或播放。
- 不支持在手机上下载 / 整理服务端文件（这些操作本来就在服务端执行）。
- `mode=emby` 时手机需能直接访问 Emby/Jellyfin 地址（URL 由服务端返回，已含 api_key）。
