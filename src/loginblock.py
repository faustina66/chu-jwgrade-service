"""登录阻断标记：密码错了就别再试。

systemd 那边靠退出码 20 + RestartPreventExitStatus 停住。但 README 也写了
用 cron 或 Windows 任务计划程序跑 `--once`，那两个东西不看退出码——密码错了
它们会每 15 分钟老老实实再试一次，一天 96 次，直到统一身份认证把号锁掉。
被锁的不只是查成绩：同一套密码还管着图书馆、校园卡、选课和学籍。

所以失败要落盘。下次启动看见标记就直接拒绝登录。

解除方式是"明确确认新密码"，不是"改完密码就自动放行"——标记里存的是密码的
HMAC。启动时即使发现密码已经变化，也必须由人工显式执行解锁命令，避免把另一个
错误密码误当成新密码。
带随机盐是因为教务密码通常不长，裸哈希拖走就能离线撞出来。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

from .redact import redact_url

log = logging.getLogger(__name__)


class LoginBlockError(RuntimeError):
    """登录阻断标记无法安全读写。"""


class LoginBlocked(LoginBlockError):
    """登录阻断标记仍在，调用方据此返回 EXIT_CONFIG_ERROR。"""


class LoginBlockCorrupt(LoginBlockError):
    """登录阻断标记存在但格式不可信，必须 fail-closed。"""


class LoginBlockWriteError(LoginBlockError):
    """登录阻断标记无法持久化，不能继续尝试登录。"""


def _digest(password: str, salt: str) -> str:
    return hmac.new(bytes.fromhex(salt), password.encode("utf-8"),
                    hashlib.sha256).hexdigest()


class LoginBlock:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _write(self, payload: dict) -> None:
        """原子写入标记，避免进程中断留下半个 JSON。"""
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp",
                dir=str(self.path.parent))
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as e:
            raise LoginBlockWriteError(
                f"无法安全写入登录阻断标记 {self.path}：{e}") from e
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("无法清理登录阻断临时文件 %s", temporary)

    @staticmethod
    def _payload(password: str, reason: str, state: str) -> dict:
        salt = secrets.token_hex(16)
        return {
            "schema_version": 1,
            "state": state,
            "salt": salt,
            "digest": _digest(password, salt),
            "reason": redact_url(reason) or "未知",
            "time": dt.datetime.now().isoformat(timespec="seconds"),
        }

    def arm(self, password: str, reason: str = "登录尝试未完成") -> None:
        """在真正发起登录前留下保护标记。

        这样登录过程中进程崩溃、网络异常或服务重启时，下一次启动也不会
        在没有人工确认的情况下继续拿同一密码撞校园网。
        """
        if self.path.exists():
            self.check(password)
        self._write(self._payload(password, reason, "armed"))

    def block(self, password: str, reason: str, *,
              needs_human: bool = False) -> None:
        """记下这次登录失败，以及当时用的是哪个密码。

        needs_human=True 用于**密码没被证伪**的拒绝：验证码、账号被临时锁定。
        这类标记解锁时不要求换密码——要求了就是条死路，人只能改一次密码
        再改回来。见 LoginNeedsHuman 的说明。
        """
        state = "human" if needs_human else "blocked"
        self._write(self._payload(password, reason, state))

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError, UnicodeError) as e:
            raise LoginBlockCorrupt(
                f"登录阻断标记 {self.path} 损坏或不可读，已拒绝登录：{e}") from e

        if not isinstance(value, dict):
            raise LoginBlockCorrupt(
                f"登录阻断标记 {self.path} 不是 JSON 对象，已拒绝登录")
        salt, digest = value.get("salt"), value.get("digest")
        state = value.get("state", "blocked")  # 兼容旧版本标记
        if (not isinstance(salt, str) or len(salt) != 32
                or not isinstance(digest, str) or len(digest) != 64
                or state not in ("armed", "human", "blocked")):
            raise LoginBlockCorrupt(
                f"登录阻断标记 {self.path} 字段不完整或不可信，已拒绝登录")
        try:
            bytes.fromhex(salt)
            int(digest, 16)
        except (TypeError, ValueError) as e:
            raise LoginBlockCorrupt(
                f"登录阻断标记 {self.path} 摘要格式不可信，已拒绝登录") from e
        return value

    def check(self, password: str) -> None:
        """启动时调用。存在阻断标记时始终拒绝，必须显式解锁。

        armed 和 blocked 都拒绝启动，但提示不同——它们是两回事：
        armed 只说明「登录发起过、结果没能确认」，不代表密码是错的。
        """
        if not self.path.exists():
            return
        d = self._read()
        if not d:
            return
        same = hmac.compare_digest(_digest(password, d["salt"]), d["digest"])

        if d.get("state", "blocked") == "armed":
            # 走到这儿说明上一次没能跑完 clear()：进程被杀、网络断在半路，
            # 或者密码提交成功了但会话没能落盘（那条路径是有意 fail-closed 的）。
            # 三种都不能推出"密码是错的"，所以解锁不该要求先改密码。
            raise LoginBlocked(
                "上次登录发起之后没能确认结果，为避免重复提交密码已阻断。"
                "常见原因：进程中途被杀，或登录成功但会话文件没能写下来。"
                + chr(10) + chr(10) +
                f"发起时间：{d.get('time', '未知')}" + chr(10) +
                f"标记原因：{d.get('reason', '未知')}" + chr(10) +
                f"阻断标记：{self.path}" + chr(10) + chr(10) +
                "确认账号能正常登录、且状态目录可写之后，执行 "
                "`python -m src.main --unlock-login` 解除。"
                "**密码没变也能解除**——这个标记不代表密码是错的。")

        if d.get("state", "blocked") == "human":
            # 验证码、账号被临时锁定。密码可能完全是对的——别让人去改密码。
            raise LoginBlocked(
                "上次认证被拒，但**不是密码的问题**（验证码或账号被临时锁定），"
                "已阻断以免继续撞。" + chr(10) + chr(10) +
                f"发生时间：{d.get('time', '未知')}" + chr(10) +
                f"标记原因：{d.get('reason', '未知')}" + chr(10) +
                f"阻断标记：{self.path}" + chr(10) + chr(10) +
                "去 https://ids.chd.edu.cn 手动登录一次（过掉验证码 / 等锁定解除），"
                "然后执行 `python -m src.main --unlock-login` 恢复。"
                "**密码不用改，原密码就能解锁。**")

        if not same:
            raise LoginBlocked(
                "登录阻断标记仍然存在，当前密码与上次失败密码不同；"
                "请先显式执行 `python -m src.main --unlock-login`，确认新密码后再恢复。"
                + chr(10) + chr(10) +
                f"失败时间：{d.get('time', '未知')}" + chr(10) +
                f"失败原因：{d.get('reason', '未知')}" + chr(10) +
                f"阻断标记：{self.path}")

        raise LoginBlocked(
            "上次登录失败后已阻断，密码至今没变，本次不再尝试——"
            "继续用错误密码重试会把统一身份认证账号锁死。" + chr(10) + chr(10) +
            f"失败时间：{d.get('time', '未知')}" + chr(10) +
            f"失败原因：{d.get('reason', '未知')}" + chr(10) + chr(10) +
            "请确认已更新为正确密码后执行 `python -m src.main --unlock-login`；"
            f"不要直接删除 {self.path}")

    def unlock(self, password: str) -> None:
        """显式解除阻断。armed 和 blocked 待遇不同。

        blocked 是「确认登录失败」，多半是密码错了，所以必须先换成另一个
        密码才放行——否则解锁就退化成"重试一次"，账号照样会被撞锁。

        armed 是「发起了但结果不明」，密码从没被证伪过。要求它换密码是
        无解的：磁盘写失败留下 armed 标记之后，check() 让你去 --unlock-login，
        而 unlock() 又说密码相同不能解锁，两条提示互相指着对方，
        唯一出路只剩下删文件——而 check() 的提示恰恰写着不要删。

        human 是「确认被拒，但不关密码的事」：验证码、账号被临时锁定。
        和 armed 一样，密码从没被证伪过，所以同样允许原密码解锁。
        2026-08-20 之前它被归进 blocked，于是碰上验证码的人只能先改一次
        密码再改回来——同样是死路，只是绕得更远。
        """
        if not self.path.exists():
            return
        d = self._read()
        if not d:
            return
        state = d.get("state", "blocked")
        if state in ("armed", "human"):
            self.clear()
            log.info("已解除登录中断标记（%s，未证伪密码）：%s", state, self.path)
            return
        same = hmac.compare_digest(_digest(password, d["salt"]), d["digest"])
        if same:
            raise LoginBlocked(
                "当前密码仍与上次失败时使用的密码相同，不能解锁；"
                "请确认密码已修改且配置或密钥链中的密码已同步。")
        self.clear()
        log.info("已显式解除登录阻断：%s", self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
