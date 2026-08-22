"""假适配器：从本地 JSON 读成绩，用来在接通真实教务系统之前跑通全链路。

用法：编辑 data/mock_grades.json，改一门课的 score，再跑一次就能看到推送。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Grade
from .base import Adapter

_SAMPLE = [
    {"term": "2025-2026-1", "course_id": "MATH101", "course_name": "高等数学(下)",
     "score": "", "credit": "5.0", "gpa": ""},
    {"term": "2025-2026-1", "course_id": "PHYS201", "course_name": "大学物理",
     "score": "88", "credit": "4.0", "gpa": "3.8"},
]


class MockAdapter(Adapter):
    name = "mock"

    def login(self) -> None:
        pass

    def fetch_grades(self) -> list[Grade]:
        # 落点由上层给。按 cwd 解析的话，从哪个目录跑就在哪儿 mkdir 出一个
        # data/——自测时随手跑一次，就在家目录里留下一个空壳目录。
        p = Path(self.cfg.get("state_dir") or "data") / "mock_grades.json"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_SAMPLE, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = json.loads(p.read_text(encoding="utf-8"))
        return [Grade.from_dict(r) for r in rows]
