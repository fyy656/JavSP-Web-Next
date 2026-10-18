"""局域网访问支持：监听地址决策、本机地址枚举与 Windows 防火墙规则管理。

设计约束：
- 默认只监听 127.0.0.1；只有在用户显式开启“允许局域网访问”后才绑定 0.0.0.0。
- 防火墙规则按端口命名，便于查询与移除；添加规则需要管理员权限，失败时给出
  可手动执行的命令，不尝试自提权。
- 非 Windows 平台不自动改防火墙，只返回状态与指引。
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess

from . import storage

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8090
_FIREWALL_RULE_PREFIX = "JavSP WEB LAN "
_NETSH_TIMEOUT = 10


class FirewallError(RuntimeError):
    """防火墙规则操作失败（通常是缺少管理员权限）。"""


def is_windows() -> bool:
    return os.name == "nt"


def lan_enabled() -> bool:
    return bool(storage.get_network_settings().get("lan_access"))


def effective_host() -> str:
    """程序应当绑定的地址：开启局域网访问时监听所有网卡，否则仅本机。"""
    return "0.0.0.0" if lan_enabled() else "127.0.0.1"


def listen_port() -> int:
    """服务端口；与 app_supervisor 一致地优先读取环境变量。"""
    raw = str(os.environ.get("JAVSP_WEB_PORT", "") or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            logger.warning("JAVSP_WEB_PORT 不是有效端口号：%r，使用默认端口 %d", raw, DEFAULT_PORT)
        else:
            if 1 <= port <= 65535:
                return port
    return DEFAULT_PORT


def _sorted_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _address_via_udp() -> list[str]:
    """通过 UDP 套接字查询系统选路后得到的主用地址（不实际发送数据）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1，不会产生真实流量
            return [probe.getsockname()[0]]
    except OSError:
        return []


def lan_addresses(port: int | None = None) -> list[str]:
    """枚举本机可用于局域网访问的 HTTP 地址（排除回环与链路本地地址）。"""
    port = listen_port() if port is None else port
    hosts: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            hosts.append(info[4][0])
    except OSError as exc:
        logger.debug("枚举本机地址失败：%s", exc)
    hosts.extend(_address_via_udp())
    addresses = []
    for host in _sorted_unique(hosts):
        if not host or host.startswith("127.") or host.startswith("169.254."):
            continue
        addresses.append(f"http://{host}:{port}")
    return addresses


# ---------------------------------------------------------------------------
# Windows 防火墙

def firewall_rule_name(port: int | None = None) -> str:
    return f"{_FIREWALL_RULE_PREFIX}{listen_port() if port is None else port}"


def _netsh(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["netsh", *arguments], capture_output=True, timeout=_NETSH_TIMEOUT,
                          errors="replace")


def firewall_rule_exists(port: int | None = None) -> bool:
    """按名称查询入站规则是否存在。非 Windows 或无权限时返回 False。"""
    if not is_windows():
        return False
    try:
        result = _netsh(["advfirewall", "firewall", "show", "rule", f"name={firewall_rule_name(port)}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("查询防火墙规则失败：%s", exc)
        return False
    return result.returncode == 0 and firewall_rule_name(port) in (result.stdout or "")


def firewall_add_rule(port: int | None = None) -> None:
    """添加放行端口的入站规则；需要管理员权限，失败时抛出带指引的错误。"""
    if not is_windows():
        raise FirewallError("当前系统不是 Windows，请手动放行端口")
    name = firewall_rule_name(port)
    try:
        result = _netsh(["advfirewall", "firewall", "add", "rule", f"name={name}",
                         "dir=in", "action=allow", "protocol=TCP", f"localport={listen_port() if port is None else port}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FirewallError(f"无法执行防火墙命令：{exc}") from exc
    if result.returncode != 0:
        raise FirewallError(
            "添加防火墙规则失败，通常需要管理员权限。请以管理员身份运行程序，"
            f"或在管理员 PowerShell 中执行：netsh advfirewall firewall add rule "
            f"name=\"{name}\" dir=in action=allow protocol=TCP localport={listen_port() if port is None else port}")


def firewall_remove_rule(port: int | None = None) -> bool:
    """尽力移除规则；规则不存在或删除失败都只记录日志。"""
    if not is_windows() or not firewall_rule_exists(port):
        return False
    try:
        result = _netsh(["advfirewall", "firewall", "delete", "rule", f"name={firewall_rule_name(port)}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("删除防火墙规则失败：%s", exc)
        return False
    return result.returncode == 0


def firewall_status(port: int | None = None) -> dict:
    """面向界面的防火墙状态：是否支持自动配置、规则是否已存在、提示文案。"""
    if not is_windows():
        return {"supported": False, "exists": False,
                "message": "非 Windows 系统请手动放行端口，例如 ufw allow 8090/tcp"}
    exists = firewall_rule_exists(port)
    return {"supported": True, "exists": exists,
            "message": "防火墙规则已就绪，局域网设备可直接访问。" if exists
            else "尚未添加防火墙规则，保存设置时将自动添加（需要管理员权限）。"}


def apply_lan_access(enabled: bool, port: int | None = None) -> dict:
    """开启/关闭局域网访问时同步维护防火墙规则，返回界面可展示的结果。"""
    status = firewall_status(port)
    if not enabled:
        removed = firewall_remove_rule(port)
        status["exists"] = False
        status["message"] = "已关闭局域网访问" + ("，防火墙规则已移除。" if removed else "。")
        return status
    if status["exists"]:
        return status
    try:
        firewall_add_rule(port)
    except FirewallError as exc:
        status["error"] = str(exc)
        status["message"] = f"局域网访问已开启，但防火墙规则添加失败：{exc}"
        return status
    status["exists"] = True
    status["message"] = "局域网访问已开启，防火墙规则已添加。"
    return status
