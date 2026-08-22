"""密码的获取与存放。

优先级：系统密钥链 > 环境变量 > 配置文件（不推荐，仅为兼容保留）。

系统密钥链就是 Windows 凭据管理器 / macOS 钥匙串 / Linux Secret Service，
由操作系统加密保管，不会以明文躺在项目目录里被误提交或被同步到网盘。
"""
from __future__ import annotations

import getpass
import logging
import os

log = logging.getLogger(__name__)

SERVICE = "jw-grade-monitor"

try:
    import keyring
except ImportError:  # 没装 keyring 也要能跑，退回环境变量
    keyring = None


def _from_keyring(username: str) -> str | None:
    if keyring is None or not username:
        return None
    try:
        return keyring.get_password(SERVICE, username)
    except Exception as e:    # noqa: BLE001 —— keyring 后端五花八门，抛什么全看装了哪个
        # Linux 无桌面环境时后端可能不可用，不该因此崩掉
        log.debug("读取密钥链失败，跳过: %s", e)
        return None


def resolve(username: str, config_password: str = "") -> tuple[str, str]:
    """返回 (密码, 来源说明)。找不到就抛异常，绝不静默用空密码去登录。"""
    pwd = _from_keyring(username)
    if pwd:
        return pwd, "系统密钥链"

    pwd = os.environ.get("JW_PASSWORD", "")
    if pwd:
        return pwd, "环境变量 JW_PASSWORD"

    if config_password:
        log.warning(
            "正在使用配置文件里的明文密码。建议改用密钥链：python -m src.main --set-password"
        )
        return config_password, "配置文件（明文，不推荐）"

    raise ValueError(
        "没有找到密码。请任选一种方式：\n"
        "  1. python -m src.main --set-password   （推荐，存进系统密钥链）\n"
        "  2. 设置环境变量 JW_PASSWORD"
    )


def store_interactive(username: str) -> int:
    """把密码存进系统密钥链。

    密码通过 getpass 从终端直接读取，不回显、不进命令历史、不写日志。
    """
    if keyring is None:
        print("未安装 keyring，请先执行：pip install keyring")
        return 1
    if not username:
        print("请先在 config.yaml 里填写 account.username")
        return 1

    print(f"为账号 {username} 设置密码（输入时不显示，直接回车确认）")
    pwd = getpass.getpass("密码: ")
    if not pwd:
        print("密码为空，已取消")
        return 1
    if pwd != getpass.getpass("再输一次: "):
        print("两次输入不一致，已取消")
        return 1

    try:
        keyring.set_password(SERVICE, username, pwd)
    except Exception as e:    # noqa: BLE001 —— keyring 后端五花八门，抛什么全看装了哪个
        print(f"写入密钥链失败: {e}")
        return 1
    print(f"已存入系统密钥链（服务名 {SERVICE}）。现在可以把 config.yaml 里的 password 留空了。")
    return 0


def clear(username: str) -> int:
    if keyring is None:
        return 1
    try:
        keyring.delete_password(SERVICE, username)
        print("已从密钥链删除")
        return 0
    except Exception as e:    # noqa: BLE001 —— keyring 后端五花八门，抛什么全看装了哪个
        print(f"删除失败（可能本来就没存）: {e}")
        return 1
