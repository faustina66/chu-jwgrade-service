"""登录频率硬闸。

认证动作分两类：换票只访问认证入口，不提交密码；完整登录才会提交密码。
两类动作分别计数：换票限制短时间爆发，密码提交限制 24 小时总量。

计数文件属于安全状态。文件不存在表示尚未记录；文件损坏或无法读写时必须
fail-closed，不能把限速器悄悄变成摆设。
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOUR = 3600.0
DAY = 86400.0
WINDOW = HOUR  # 兼容旧版调用方；新的代码使用 HOUR / DAY。

TICKET = "ticket"
PASSWORD = "password"


def _preserve_owner(target: Path, temporary: Path) -> None:
    """让文件属主保持不变，避免 root 写一次就把它从服务用户手里抢走。

    服务以 jwgrade 跑，诊断脚本以 root 跑。root 写完之后文件变成 root 所有，
    服务用户再也写不进去——而写失败是 fail-closed 的，监控会直接停机。
    一个排错动作把监控搞停，这代价太蠢了。

    文件还不存在时（比如 root 第一次跑诊断就把它创建出来）就跟着所在目录的
    属主走，那个目录本来就是服务用户的。
    """
    if not hasattr(os, "chown"):
        return                     # Windows 没有属主概念
    try:
        st = target.stat()
    except OSError:
        try:
            st = target.parent.stat()   # 新建：跟着 data/ 目录的属主
        except OSError:
            return
    try:
        os.chown(temporary, st.st_uid, st.st_gid)
    except (OSError, AttributeError):
        pass                       # 非 root 时改不了别人的属主，忽略
class LoginRateLimited(RuntimeError):
    """这一轮不允许继续认证，等待窗口过去后再恢复。"""


class LoginRateStateError(RuntimeError):
    """登录频率状态无法安全读写，必须停止认证。"""


class LoginRate:
    def __init__(self, path: str | Path, per_hour: int = 3,
                 password_per_day: int = 1):
        self.path = Path(path)
        self.per_hour = max(1, int(per_hour))
        self.password_per_day = max(1, int(password_per_day))

    # ------------------------------------------------------------- 落盘

    def _read(self) -> list[tuple[float, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            raise LoginRateStateError(
                f"登录频率计数文件 {self.path} 损坏或不可读，已拒绝继续认证：{e}") from e

        if not isinstance(data, list):
            raise LoginRateStateError(
                f"登录频率计数文件 {self.path} 不是数组，已拒绝继续认证")

        cutoff = time.time() - DAY
        out: list[tuple[float, str]] = []
        for index, item in enumerate(data):
            if isinstance(item, dict):
                timestamp, kind = item.get("t"), item.get("k")
                if kind not in (TICKET, PASSWORD):
                    raise LoginRateStateError(
                        f"登录频率计数文件 {self.path} 第 {index + 1} 项类型不可信")
            else:
                # 旧格式是裸时间戳。按 password 收下，宁可算重不算轻。
                timestamp, kind = item, PASSWORD

            if (type(timestamp) not in (int, float)
                    or not math.isfinite(float(timestamp))):
                raise LoginRateStateError(
                    f"登录频率计数文件 {self.path} 第 {index + 1} 项时间戳不可信")
            timestamp = float(timestamp)
            if timestamp > cutoff:
                out.append((timestamp, kind))
        return sorted(out)

    def _write(self, entries: list[tuple[float, str]]) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                json.dump([{"t": round(t, 3), "k": k} for t, k in entries], f)
                f.flush()
                os.fsync(f.fileno())
            _preserve_owner(self.path, temporary)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as e:
            raise LoginRateStateError(
                f"登录频率计数文件 {self.path} 无法安全写入：{e}") from e
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("无法清理登录频率临时文件 %s", temporary)

    # ------------------------------------------------------------- 查与记

    def _password_exhausted(self, now: float) -> tuple[int, int] | None:
        """24 小时内的密码额度用完了没有。用完则返回 (已用次数, 还要等几秒)。"""
        recent = [t for t, kind in self._read()
                  if kind == PASSWORD and t > now - DAY]
        if len(recent) < self.password_per_day:
            return None
        return len(recent), max(int(min(recent) + DAY - now), 0)

    def check_attempt(self) -> None:
        """任何认证动作之前调用，限制一小时内的认证爆发。"""
        now = time.time()
        recent = [t for t, _ in self._read() if t > now - HOUR]
        if len(recent) < self.per_hour:
            return
        wait = int(min(recent) + HOUR - now)
        message = (
            f"最近一小时已认证 {len(recent)} 次，达到上限 {self.per_hour} 次，"
            f"本轮跳过；约 {max(wait, 0) // 60 + 1} 分钟后可再试。"
            "这是为了避免认证服务器判定频繁登录。")
        # 日额度也满了的话，那才是真正的墙。只报小时闸会让人白等一小时回来，
        # 再撞上一堵没预告过的「约 22 小时」——两道闸挂在认证流程的前后两处，
        # 小时闸永远先说话，所以只能由它把后面那堵一并说出来。
        #
        # 说「如果」是因为这一刻还不知道这轮要不要密码：换票成功的轮次根本
        # 不消耗日额度。把话说死会在换票能成功时误导人。
        blocked = self._password_exhausted(now)
        if blocked:
            used, day_wait = blocked
            message += (
                f" 另外，24 小时内的密码额度也已用完（{used}/{self.password_per_day}）；"
                f"如果这一轮需要重新提交密码，还要等约 {day_wait // 3600 + 1} 小时。")
        raise LoginRateLimited(message)

    def check_password(self) -> None:
        """真正提交密码之前调用，限制 24 小时内的完整登录次数。"""
        blocked = self._password_exhausted(time.time())
        if blocked is None:
            return
        used, wait = blocked
        raise LoginRateLimited(
            f"最近 24 小时已完整登录 {used} 次，达到上限 "
            f"{self.password_per_day} 次，本轮跳过；"
            f"约 {wait // 3600 + 1} 小时后可再试。")

    def note(self, kind: str = PASSWORD) -> None:
        """认证动作已经发出前记录一次；读写失败必须向上抛出。"""
        if kind not in (TICKET, PASSWORD):
            raise ValueError(f"未知认证动作类型：{kind!r}")
        entries = self._read()
        entries.append((time.time(), kind))
        try:
            self._write(entries)
        except LoginRateStateError as e:
            log.error("登录频率计数写入失败 %s：%s", self.path, e)
            raise
        except OSError as e:
            error = LoginRateStateError(
                f"登录频率计数文件 {self.path} 无法安全写入：{e}")
            log.error("登录频率计数写入失败 %s：%s", self.path, error)
            raise error from e

    # ------------------------------------------------------------- 兼容与观察

    def check(self) -> None:
        """旧版兼容别名：检查一小时认证爆发限制。"""
        self.check_attempt()

    def recent_hour(self) -> int:
        now = time.time()
        return sum(1 for t, _ in self._read() if t > now - HOUR)

    def recent(self) -> int:
        """旧版兼容别名：返回最近一小时认证次数。"""
        return self.recent_hour()

    def passwords_today(self) -> int:
        now = time.time()
        return sum(1 for t, kind in self._read()
                   if kind == PASSWORD and t > now - DAY)
