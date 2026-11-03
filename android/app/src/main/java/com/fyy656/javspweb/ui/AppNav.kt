package com.fyy656.javspweb.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.fyy656.javspweb.JavspApp
import com.fyy656.javspweb.ui.detail.DetailScreen
import com.fyy656.javspweb.ui.library.LibraryScreen
import com.fyy656.javspweb.ui.login.LoginScreen
import com.fyy656.javspweb.ui.player.PlayerScreen
import java.net.URLEncoder

object Routes {
    const val LOGIN = "login"
    const val LIBRARY = "library"
    const val DETAIL = "detail/{filmId}"
    const val PLAYER = "player/{filmId}/{start}/{seriesId}/{season}/{index}"

    fun detail(filmId: String) = "detail/${enc(filmId)}"

    @Suppress("LongParameterList")
    fun player(
        filmId: String,
        start: Int,
        seriesId: String,
        season: Int,
        index: Int,
    ): String {
        val safeSeries = enc(seriesId.ifBlank { "-" })
        return "player/${enc(filmId)}/$start/$safeSeries/$season/$index"
    }

    private fun enc(v: String) = URLEncoder.encode(v, "UTF-8").replace("+", "%20")
}

@Composable
fun AppNav(app: JavspApp, hasServer: Boolean) {
    val navController = rememberNavController()

    // 服务器被清空（退出登录）时回到登录页
    LaunchedEffect(hasServer) {
        if (!hasServer) {
            navController.navigate(Routes.LOGIN) {
                popUpTo(0) { inclusive = true }
            }
        }
    }

    NavHost(
        navController = navController,
        startDestination = if (hasServer) Routes.LIBRARY else Routes.LOGIN,
    ) {
            composable(Routes.LOGIN) {
                LoginScreen(onLoggedIn = {
                    navController.navigate(Routes.LIBRARY) {
                        popUpTo(0) { inclusive = true }
                    }
                })
            }

            composable(Routes.LIBRARY) {
                LibraryScreen(
                    onOpenFilm = { filmId -> navController.navigate(Routes.detail(filmId)) },
                    onLoggedOut = {
                        navController.navigate(Routes.LOGIN) {
                            popUpTo(0) { inclusive = true }
                        }
                    },
                )
            }

            composable(
                Routes.DETAIL,
                arguments = listOf(navArgument("filmId") { type = NavType.StringType }),
            ) { backStack ->
                val filmId = backStack.arguments?.getString("filmId").orEmpty()
                DetailScreen(
                    filmId = filmId,
                    onBack = { navController.popBackStack() },
                    onPlay = { id, _, start, seriesId, season, index ->
                        navController.navigate(Routes.player(id, start, seriesId, season, index))
                    },
                )
            }

            composable(
                Routes.PLAYER,
                arguments = listOf(
                    navArgument("filmId") { type = NavType.StringType },
                    navArgument("start") { type = NavType.IntType; defaultValue = 0 },
                    navArgument("seriesId") { type = NavType.StringType; defaultValue = "-" },
                    navArgument("season") { type = NavType.IntType; defaultValue = 0 },
                    navArgument("index") { type = NavType.IntType; defaultValue = -1 },
                ),
            ) { backStack ->
                val args = backStack.arguments
                PlayerScreen(
                    filmId = args?.getString("filmId").orEmpty(),
                    startSeconds = args?.getInt("start") ?: 0,
                    seriesId = args?.getString("seriesId")?.takeIf { it != "-" }.orEmpty(),
                    season = args?.getInt("season") ?: 0,
                    currentIndex = args?.getInt("index") ?: -1,
                    onExit = { navController.popBackStack() },
                )
            }
        }
}
