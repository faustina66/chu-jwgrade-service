"""教务系统适配器基类。

各学校登录方式差异极大（CAS 单点、直连、WebVPN），但对外只需要提供
「登录」和「取成绩」两个能力。上层调度逻辑完全不关心是哪所学校。
"""
from __future__ import annotations

import logging

import requests

from ..models import Grade

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class SessionExpired(Exception):
    """会话失效，需要重新登录。"""


class LoginFailed(Exception):
    """登录失败且重试无意义（如密码错误），应停止并告警。"""


class LoginNeedsHuman(LoginFailed):
    """认证被明确拒绝，但**和密码对不对无关**：验证码、账号被临时锁定。

    继承 LoginFailed 是因为处理方式一样——立刻停机、告警、绝不重试。
    区别只在**怎么恢复**：这类失败不该要求先改密码，因为密码从没被证伪过。
    人在网页版把验证码过了 / 等锁定自己解除之后，拿原密码就该能恢复。

    2026-08-20 之前这两种都走 LoginFailed，落盘成 blocked 状态，而 blocked
    的解锁条件是"必须换一个不同的密码"——于是碰上验证码的人只能先改一次
    密码再改回来。那是条死路。
    """


class LoginTransient(Exception):
    """登录链路暂时不可用或页面改版，重试有意义，不写密码阻断标记。"""


class Adapter:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.session = requests.Session()
        # 只设 UA 是不够的：requests 默认发 Accept: */*，和浏览器 UA 摆在一起
        # 是很明显的机器人特征，容易被安全设备挑出来。补齐成浏览器的样子。
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        })
        self._logged_in = False
        self._on_password_gate = None
        self._on_password_submit = None
        self._on_ticket_start = None
        self._on_ticket_success = None

    def resume_from_cookies(self) -> bool:
        """cookie 刚从磁盘装回来。看着还能用就返回 True 并跳过登录。

        猜错的代价只是一次多余的抓取请求——会话真死了，取成绩时会被重定向
        回认证页，SessionExpired 会把登录流程正常带起来。而猜对的收益是
        重启完全不碰认证服务器。
        """
        return False

    def login(self) -> None:
        raise NotImplementedError

    def fetch_grades(self) -> list[Grade]:
        raise NotImplementedError

    def run(self, on_login_start=None, on_login_success=None,
            on_login_failure=None, on_password_submit=None,
            on_password_gate=None,
            on_ticket_start=None, on_ticket_success=None) -> list[Grade]:
        """取成绩的统一入口：复用会话，失效才重登。

        每轮都重新登录会显著增加账号异常风险，所以默认沿用已有 session，
        只有明确检测到会话过期时才重新走登录流程。两个可选钩子用于在
        真正发起登录前后更新持久化安全状态。
        """
        def login_once() -> None:
            old_hooks = (self._on_password_gate,
                         self._on_password_submit, self._on_ticket_start,
                         self._on_ticket_success)
            self._on_password_gate = on_password_gate
            self._on_password_submit = on_password_submit
            self._on_ticket_start = on_ticket_start
            self._on_ticket_success = on_ticket_success
            try:
                if on_login_start:
                    on_login_start()
                try:
                    self.login()
                except LoginFailed:
                    # 这类失败会由上层落盘为阻断标记，不能在这里清掉。
                    raise
                except Exception:
                    # 只有密码提交前的临时异常才适合清理旧标记。真实登录流程
                    # 的安全钩子在提交密码前才建立标记，主流程不会传入清理钩子，
                    # 因此提交后的异常会保留标记，避免下一轮再次交密码。
                    if on_login_failure:
                        on_login_failure()
                    raise
                self._logged_in = True
                if on_login_success:
                    on_login_success()
            finally:
                (self._on_password_gate,
                 self._on_password_submit, self._on_ticket_start,
                 self._on_ticket_success) = old_hooks

        if not self._logged_in:
            login_once()
        try:
            return self.fetch_grades()
        except SessionExpired:
            log.info("会话已过期，重新登录")
            self._logged_in = False
            login_once()
            return self.fetch_grades()

    def _notify_password_gate(self) -> None:
        """确定这一轮要提交密码时调用，只查额度，不产生任何副作用。

        必须早于「取登录页」和「带学号的验证码探测」这两个请求——额度用完时
        它们本就不该发出去，而带学号那个正是风控最敏感的信号。
        """
        if self._on_password_gate:
            self._on_password_gate()

    def _notify_password_submit(self) -> None:
        """通知上层：下一步即将提交密码。"""
        if self._on_password_submit:
            self._on_password_submit()

    def _notify_ticket_start(self) -> None:
        """通知上层：开始尝试复用长期认证票据。"""
        if self._on_ticket_start:
            self._on_ticket_start()

    def _notify_ticket_success(self) -> None:
        """通知上层：长期票据已成功换成教务会话。"""
        if self._on_ticket_success:
            self._on_ticket_success()
