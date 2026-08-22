"""定期的存活信号。

程序静默停止时，表面现象与近期没有新成绩相同，不容易及时发现。服务器重启后
服务未启动、进程因内存不足被终止或 systemd 状态异常时，停止告警也可能无法发送。

因此，如果长时间没有发送任何通知，程序会主动发送一条心跳消息。
发送其他通知后，心跳计时重新开始。

状态使用空文件的 mtime 表示，因为这里只需记录一个时间戳。这样无需为该状态
增加 JSON 结构校验和损坏文件封存逻辑。
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
            # 心跳通知失败不应影响主流程，只记录日志。
            log.warning("无法更新存活标记 %s：%s", self.path, e)

    def due(self) -> bool:
        """距离上次推送是否已经超过设定天数。"""
        if not self.enabled:
            return False
        try:
            last = self.path.stat().st_mtime
        except OSError:
            # 首次运行仅初始化计时器，不立即发送心跳通知。
            self.note_push()
            return False
        return (time.time() - last) >= self.days * 86400
