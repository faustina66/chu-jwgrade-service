"""轮询节奏。

出分是高度集中的：期末后那几天教务处一批一批地发，平时几个月纹丝不动。
固定间隔要么平时白白浪费请求，要么出分季拿到得太慢，所以做成三档自适应。

用离散三档而不是连续调节，是为了随时能一眼看出"现在处于哪一档、为什么"。
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

ACTIVE, NORMAL, IDLE = "active", "normal", "idle"
LABEL = {ACTIVE: "加速档", NORMAL: "常规档", IDLE: "省电档"}


@dataclass
class ScheduleConfig:
    adaptive: bool = True
    interval: int = 1800            # 常规档
    active_interval: int = 900      # 刚出分时的加速档
    idle_interval: int = 3600       # 长期无动静的省电档
    active_duration: int = 7200     # 加速持续多久（秒），期间无新变化则回落
    idle_after: int = 86400         # 多久没变化后进入省电档（秒）
    jitter: int = 180
    quiet_hours: tuple = ()
    fail_alert_after: int = 5

    @classmethod
    def from_dict(cls, d: dict) -> ScheduleConfig:
        return cls(
            adaptive=bool(d.get("adaptive", True)),
            interval=int(d.get("interval_seconds", 1800)),
            active_interval=int(d.get("active_interval_seconds", 900)),
            idle_interval=int(d.get("idle_interval_seconds", 3600)),
            active_duration=int(d.get("active_duration_minutes", 120)) * 60,
            idle_after=int(d.get("idle_after_hours", 24)) * 3600,
            jitter=int(d.get("jitter_seconds", 180)),
            quiet_hours=tuple(d.get("quiet_hours") or ()),
            fail_alert_after=int(d.get("fail_alert_after", 5)),
        )

    def describe(self) -> str:
        if not self.adaptive:
            return f"固定 {self.interval}s"
        return (f"自适应 加速{self.active_interval}s / 常规{self.interval}s / "
                f"省电{self.idle_interval}s")


class AdaptiveScheduler:
    def __init__(self, cfg: ScheduleConfig):
        self.cfg = cfg
        self._started = time.time()
        self._last_change: float | None = None

    def reload(self, cfg: ScheduleConfig) -> None:
        """热更新调度参数，保留已有的出分节奏状态。"""
        self.cfg = cfg

    def note(self, changed: bool) -> None:
        if changed:
            self._last_change = time.time()

    @property
    def mode(self) -> str:
        if not self.cfg.adaptive:
            return NORMAL
        now = time.time()
        # 刚抓到变化 → 加速。启动本身不算"有动静"，否则每次重启都会白白加速。
        if self._last_change is not None and now - self._last_change < self.cfg.active_duration:
            return ACTIVE
        quiet_since = self._last_change or self._started
        if now - quiet_since >= self.cfg.idle_after:
            return IDLE
        return NORMAL

    def base_interval(self) -> int:
        return {
            ACTIVE: self.cfg.active_interval,
            NORMAL: self.cfg.interval,
            IDLE: self.cfg.idle_interval,
        }[self.mode]

    def next_delay(self) -> tuple[int, str]:
        """返回 (等待秒数, 档位说明)。抖动是为了不打出固定节奏。"""
        base = self.base_interval()
        jitter = min(self.cfg.jitter, base // 3)   # 抖动别超过基数的 1/3
        delay = base + random.randint(-jitter, jitter) if jitter > 0 else base
        return max(60, delay), LABEL[self.mode]
