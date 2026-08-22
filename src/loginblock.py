"""登录阻断标记：密码错误后停止自动重试。

systemd 那边靠退出码 20 + RestartPreventExitStatus 停住。但 README 也写了
用 cron 或 Windows 任务计划程序运行 `--once` 时，调度器可能不会根据退出码停止任务。
如果不持久化阻断状态，错误密码可能被定时重复提交，最终导致账号被锁定。
被锁的不只是查成绩：同一套密码还管着图书馆、校园卡、选课和学籍。

所以失败要落盘。下次启动看见标记就直接拒绝登录。

解除方式是"明确确认新密码"，不是"改完密码就自动放行"——标记里存的是密码的
HMAC。启动时即使发现密码已经变化，也必须由人工显式执行解锁命令，避免把另一个
错误密码误当成新密码。
随机盐用于降低摘要泄露后遭受离线穷举的风险。
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
        在没有人工确认的情况下继续向统一身份认证提交同一密码。
        """
        if self.path.exists():
            self.check(password)
        self._write(self._payload(password, reason, "armed"))

    def block(self, password: str, reason: str, *,
              needs_human: bool = False) -> None:
        """记下这次登录失败，以及当时用的是哪个密码。

        needs_human=True 用于无法确认密码错误的拒绝，例如验证码或账号临时锁定。
        这类标记解锁时不要求更换密码，详见 LoginNeedsHuman 的说明。
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
            # 验证码或账号临时锁定并不能证明密码错误，因此不要求用户更改密码。
            raise LoginBlocked(
                "上次认证被拒，但**不是密码的问题**（需要验证码或账号被临时锁定），"
                "程序已阻止后续认证尝试。" + chr(10) + chr(10) +
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
            "继续使用错误密码重试可能导致统一身份认证账号被锁定。" + chr(10) + chr(10) +
            f"失败时间：{d.get('time', '未知')}" + chr(10) +
            f"失败原因：{d.get('reason', '未知')}" + chr(10) + chr(10) +
            "请确认已更新为正确密码后执行 `python -m src.main --unlock-login`；"
            f"不要直接删除 {self.path}")

    def unlock(self, password: str) -> None:
        """显式解除阻断。armed 和 blocked 待遇不同。

        blocked 表示已确认登录失败，通常由密码错误引起，因此必须先更换密码。
        否则解除阻断等同于允许再次提交同一错误密码，仍可能导致账号锁定。

        armed 表示已发起登录但结果不明，不能据此判断密码错误，因此允许使用
        原密码解除阻断，避免因磁盘写入失败等情况造成无法恢复的阻断状态。

        human 表示认证因验证码或账号临时锁定而被拒绝，同样不能据此判断密码
        错误，因此也允许使用原密码解除阻断。
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
