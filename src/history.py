"""成绩变更历史。

用 append-only 的 JSONL 而不是数据库：
  - 追加写不会动到已有内容，进程被杀最多丢最后一行
  - 文件能直接用记事本打开、能 grep，不需要任何工具
  - 快照记录"现在是什么"，历史记录"什么时候变成这样的"，两者互补

每行一条变化，例如：
  {"at": "2026-08-14 19:30:12", "kind": "filled", "course_name": "C语言程序设计", ...}
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import unicodedata
from collections import deque
from pathlib import Path

from .models import Change

log = logging.getLogger(__name__)

_TEXT_FIELDS = ("at", "kind", "label", "term", "course_id", "course_name",
                "score", "old_score", "credit", "gpa")


def _row_problem(value) -> str | None:
    if not isinstance(value, dict):
        return f"应该是对象，实际是 {type(value).__name__}"
    for field_name in _TEXT_FIELDS:
        if field_name in value and type(value[field_name]) is not str:
            return (f"{field_name} 应该是字符串，实际是 "
                    f"{type(value[field_name]).__name__}")
    for required in ("at", "kind", "course_name", "score"):
        if required not in value:
            return f"缺字段 {required}"
    if not value["course_name"].strip():
        return "course_name 不能为空"
    return None


class History:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, changes: list[Change]) -> None:
        if not changes:
            return
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            # 用 os.open 指定 0600 建文件，不靠 systemd 的 UMask。
            # 这里面是你的成绩变更流水，而部署方式（systemd / cron / 手跑）
            # 各有各的 umask——权限不该取决于它是被怎么启动的。
            # mode 只在创建时生效，已存在的文件保持原样，不去改人工设过的权限。
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            with os.fdopen(os.open(self.path, flags, 0o600), "a",
                           encoding="utf-8") as f:
                for c in changes:
                    g = c.grade
                    f.write(json.dumps({
                        "at": now,
                        "kind": c.kind,
                        "label": c.label,
                        "term": g.term,
                        "course_id": g.course_id,
                        "course_name": g.course_name,
                        "score": g.score,
                        "old_score": c.old_score,
                        "credit": g.credit,
                        "gpa": g.gpa,
                    }, ensure_ascii=False) + "\n")
        except OSError as e:
            # 历史写失败不该影响推送——通知比留档重要
            log.warning("写入历史失败: %s", e)

    def read(self, limit: int = 0) -> list[dict]:
        if not self.path.exists():
            return []
        # limit > 0 时只保留最后 N 条，文件再大也不会把全部历史装进内存。
        rows = deque(maxlen=limit) if limit > 0 else []
        with self.path.open("rb") as f:
            for number, raw_line in enumerate(f, 1):
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError as e:
                    log.warning("跳过编码异常的历史第 %d 行：%s", number, e)
                    continue
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue      # 跳过写了一半的残行，不因此报错
                problem = _row_problem(value)
                if problem:
                    log.warning("跳过结构异常的历史第 %d 行：%s", number, problem)
                    continue
                rows.append(value)
        return list(rows)


def _width(s: str) -> int:
    """字符串在终端里占几格。中文、全角标点算两格，ljust 按字符数算会对不齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, target: int) -> str:
    return s + " " * max(0, target - _width(s))


def render(rows: list[dict]) -> str:
    """把历史渲染成一眼能扫的时间线。"""
    valid_rows = [r for r in rows if _row_problem(r) is None]
    if not valid_rows:
        return "还没有任何变更记录。"

    name_w = max(_width(r.get("course_name", "")) for r in valid_rows)
    label_w = max(_width(r.get("label", "")) for r in valid_rows)
    lines = []
    for r in valid_rows:
        when = r.get("at", "")[:16]          # 精确到分钟就够了
        name = _pad(r.get("course_name", ""), name_w)
        label = _pad(r.get("label", ""), label_w)
        old, new = r.get("old_score", ""), r.get("score", "")
        if r.get("kind") == "withdrawn":
            delta = f"{old} → 已撤回"
        elif old and old != new:
            delta = f"{old} → {new}"
        else:
            delta = new
        lines.append(f"{when}  {label}  {name}  {delta}")

    tally: dict[str, int] = {}
    for r in valid_rows:
        tally[r.get("label", "?")] = tally.get(r.get("label", "?"), 0) + 1
    summary = "，".join(f"{k} {v} 次" for k, v in tally.items())
    return "\n".join(lines) + f"\n\n共 {len(valid_rows)} 条：{summary}"
