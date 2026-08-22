"""跨平台的进程单实例锁。

锁文件会保留在磁盘上，真正的互斥由操作系统持有；进程崩溃或被杀后，内核会
自动释放锁，不需要靠删除一个可能过期的 PID 文件来猜进程是否还活着。
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import TextIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class InstanceAlreadyRunning(RuntimeError):
    """另一个监控进程已经持有同一把运行锁。"""


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file: TextIO | None = None

    def acquire(self) -> InstanceLock:
        if self._file is not None:
            raise RuntimeError("这把 InstanceLock 已经被当前对象持有")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+", encoding="ascii", errors="replace")
        locked = False
        try:
            # msvcrt.locking 只能锁已有字节；先保证第 1 个字节存在。
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)

            try:
                self._lock(handle)
                locked = True
            except OSError as e:
                if os.name != "nt" and e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                # Windows 的字节锁会连读取一起拒绝；PID 只是诊断信息，读不到
                # 不能掩盖真正的“已有实例”结论。
                try:
                    handle.seek(0)
                    holder = handle.read().strip() or "未知"
                except OSError:
                    holder = "未知"
                raise InstanceAlreadyRunning(
                    f"另一个成绩监控实例正在运行（PID {holder}，锁文件 {self.path}）"
                ) from e

            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            self._file = handle
            return self
        except Exception:
            if locked:
                self._unlock(handle)
            handle.close()
            raise

    @staticmethod
    def _lock(handle: TextIO) -> None:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: TextIO) -> None:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
