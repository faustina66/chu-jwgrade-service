"""把登录会话落盘，让重启不再等于一次登录。

cookie 原本只活在进程内存里，于是每次 systemctl restart 都要重新完整登录。
2026-08-16 账号被判「频繁登录」冻结，这三天里我为了部署让服务重启了八次，
每一次都是一次密码提交。

而一旦把「完整登录」限成一天一次，不落盘就成了脚枪：当天第二次重启就再也
登不进去，监控停摆一整天。

存的是完整的 cookie 属性而不是 name→value：CASTGC 在 ids.chd.edu.cn 上、
JSESSIONID 在 bkjw.chd.edu.cn 上，丢掉 domain 就送不对地方。

这个文件等同于一把能看全部成绩的钥匙，所以按 0600 建、原子写、
并且保住原属主——服务以 jwgrade 跑，诊断脚本以 root 跑，谁写都不能把
对方挡在门外。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

import requests

from .store import account_fingerprint

log = logging.getLogger(__name__)

_FIELDS = ("name", "value", "domain", "path", "secure", "expires")


class SessionStoreError(RuntimeError):
    """登录会话无法安全保存，继续运行会让重启再次触发认证。"""


class SessionStore:
    def __init__(self, path: str | Path, username: str = ""):
        self.path = Path(path)
        # 快照和发件箱都绑了学号，这份更该绑：里面是活的 CAS 会话，
        # 换账号后把旧 cookie 装回去，等于用别人的身份去查成绩。
        self.account = account_fingerprint(username)

    def save(self, session: requests.Session) -> None:
        rows = []
        for c in session.cookies:
            rows.append({f: getattr(c, f, None) for f in _FIELDS})
        if not rows:
            return
        payload = {"account": self.account, "cookies": rows}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            temporary = Path(tmp)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                _preserve_owner(self.path, temporary)
                os.replace(temporary, self.path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        except OSError as e:
            # 这不是普通缓存：存不下会让下一次重启丢掉会话，重新打认证入口。
            # 必须让上层停机，避免服务看似正常却把下一次启动变成一次登录。
            error = SessionStoreError(
                f"无法安全保存登录会话 {self.path}：{e}")
            log.error("%s", error)
            raise error from e

    def restore(self, session: requests.Session) -> int:
        """把上次的 cookie 装回去，返回恢复了几条。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            # 坏了就当没有。最坏结果是多登一次，不值得为它停机——
            # 这只是优化，不像登录阻断标记那样是安全边界。
            log.warning("会话文件读不出来，按未登录处理：%s", e)
            self.clear()
            return 0

        if isinstance(data, dict):
            recorded = data.get("account")
            rows = data.get("cookies")
            if self.account and recorded and recorded != self.account:
                log.warning("会话文件属于另一个学号，已丢弃，按未登录处理")
                self.clear()
                return 0
        else:
            rows = data            # 旧格式：裸 cookie 数组，没绑账号
        if not isinstance(rows, list):
            self.clear()
            return 0

        now = time.time()
        restored = 0
        for row in rows:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            expires = row.get("expires")
            if isinstance(expires, (int, float)) and expires < now:
                continue          # 已经过期的别装回去
            try:
                session.cookies.set_cookie(requests.cookies.create_cookie(
                    name=str(row["name"]), value=str(row.get("value") or ""),
                    domain=str(row.get("domain") or ""),
                    path=str(row.get("path") or "/"),
                    secure=bool(row.get("secure")),
                    expires=expires if isinstance(expires, (int, float)) else None,
                ))
                restored += 1
            except (TypeError, ValueError) as e:
                log.debug("跳过一条坏 cookie：%s", e)
        return restored

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("删不掉会话文件 %s：%s", self.path, e)


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
