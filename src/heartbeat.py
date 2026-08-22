"""定期的存活信号。

这个程序失效时最隐蔽的形态不是崩溃，是安静地停下来：你看到的现象是
"最近没出分"，和一切正常一模一样。服务器重启没起来、进程被 OOM 杀掉、
systemd 状态异常——这些情况下连"我停了"那条告警都发不出来。

所以需要一条反向的信号：只要长时间没给你发过任何东西，就主动报个平安。
出分季它几乎不会触发，因为总有真实通知顶着。

状态用一个空文件的 mtime 表示，不是 JSON：这里只需要记一个时间戳，
而项目里每一个 JSON 状态文件都得配一套结构校验和封存逻辑。没有内容，
就没有"合法但结构不对"这种失败模式。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, path: str | Path, days: int):
        self.path = Path(path)
        self.days = max(0, int(days or 0))
        if self.days:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.days > 0

    def note_push(self) -> None:
        """任何一条通知成功送达后调用，把计时器归零。"""
        if not self.enabled:
            return
        try:
            # touch() 的 mode 会被 umask 削掉，用 os.open 才能真的定成 0600
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
            os.utime(self.path, None)
        except OSError as e:
            # 报平安失灵不该影响主流程，记一笔就够了
            log.warning("无法更新存活标记 %s：%s", self.path, e)

    def due(self) -> bool:
        """距离上次推送是否已经超过设定天数。"""
        if not self.enabled:
            return False
        try:
            last = self.path.stat().st_mtime
        except OSError:
            # 首次运行：先把计时器起个头，别一上来就报平安
            self.note_push()
            return False
        return (time.time() - last) >= self.days * 86400
