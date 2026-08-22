"""成绩数据结构。不同教务系统的字段统一转换为这里定义的格式。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 教务系统里表示「还没出分」的各种写法
_EMPTY_SCORES = {"", "--", "-", "—", "暂无", "未出", "未发布", "none", "null"}

# 持久化文件里的 Grade 只能包含这些文本字段。运行时对象可以由适配器直接
# 构造；从磁盘恢复时则必须先经过 persisted_grade_problem()，避免错误类型
# 一路流到渲染阶段才以 AttributeError 的形式爆出来。
PERSISTED_TEXT_FIELDS = (
    "term", "course_id", "course_name", "score", "credit", "gpa", "last_score",
)
VALID_CHANGE_KINDS = frozenset({"new", "filled", "changed", "withdrawn", "republished"})


def _grade_key(term: str, course_id: str, course_name: str) -> str:
    ident = course_id.strip() or course_name.strip()
    return f"{term.strip()}::{ident}"


def persisted_grade_problem(value, path: str = "grade", *,
                            storage_key: str | None = None) -> str | None:
    """校验一条准备从持久化 JSON 恢复的成绩记录。"""
    if not isinstance(value, dict):
        return f"{path} 应该是对象，实际是 {type(value).__name__}"

    for field_name in PERSISTED_TEXT_FIELDS:
        if field_name in value and type(value[field_name]) is not str:
            return (f"{path}.{field_name} 应该是字符串，实际是 "
                    f"{type(value[field_name]).__name__}")
    for required in ("term", "course_name"):
        if required not in value:
            return f"{path} 缺字段 {required}"
    if not value["course_name"].strip():
        return f"{path}.course_name 不能为空"

    if "absent_rounds" in value and type(value["absent_rounds"]) is not int:
        return f"{path}.absent_rounds 应该是整数"
    if "withdrawn" in value and type(value["withdrawn"]) is not bool:
        return f"{path}.withdrawn 应该是布尔"

    raw = value.get("raw", {})
    if not isinstance(raw, dict):
        return f"{path}.raw 应该是对象，实际是 {type(raw).__name__}"
    for key, raw_value in raw.items():
        if type(key) is not str:
            return f"{path}.raw 的字段名应该是字符串"
        if raw_value is not None and type(raw_value) is not str:
            return (f"{path}.raw[{key!r}] 应该是字符串或 null，实际是 "
                    f"{type(raw_value).__name__}")
    if storage_key is not None:
        expected = _grade_key(
            value["term"], value.get("course_id", ""), value["course_name"])
        if storage_key != expected:
            return (f"{path} 的映射键是 {storage_key!r}，"
                    f"按课程字段计算应为 {expected!r}")
    return None


@dataclass
class Grade:
    term: str                 # 学期，如 2025-2026-1
    course_id: str            # 课程代码或课程序号
    course_name: str
    score: str = ""           # 成绩，可能是数字，也可能是"优秀"/"通过"
    credit: str = ""
    gpa: str = ""             # 绩点
    # 被撤回前的分数。成绩重新发布时靠它判断分数到底改没改
    last_score: str = ""
    # 连续几轮没在成绩页上抓到这门课。长安大学的成绩是整行出现、整行消失的，
    # 所以"行不见了"才是撤回的信号，而不是"分数栏空掉"。但页面偶尔会返回
    # 残缺内容，所以要连着几轮都不见才算数。
    absent_rounds: int = 0
    # 已确认撤回并且通知过了。留着这条记录而不是删掉：删了的话下一轮它又变成
    # "新消失的"，会天天推；留着还能认出后来的重新发布。
    withdrawn: bool = False
    raw: dict[str, Any] = field(default_factory=dict)   # 原始行，方便排查

    @property
    def key(self) -> str:
        """课程唯一标识。少数系统不提供课程代码，此时使用课程名称。"""
        return _grade_key(self.term, self.course_id, self.course_name)

    @property
    def has_score(self) -> bool:
        return self.score.strip().lower() not in _EMPTY_SCORES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Grade:
        return cls(
            term=str(d.get("term", "")),
            course_id=str(d.get("course_id", "")),
            course_name=str(d.get("course_name", "")),
            score=str(d.get("score", "")),
            credit=str(d.get("credit", "")),
            gpa=str(d.get("gpa", "")),
            last_score=str(d.get("last_score", "")),
            absent_rounds=int(d.get("absent_rounds", 0) or 0),
            withdrawn=bool(d.get("withdrawn", False)),
            raw=d.get("raw") or {},
        )


@dataclass
class Change:
    """一次 diff 出来的变化。

    kind:
        new         新课程直接带分出现
        filled      课程行早就在，成绩栏从空变成有分（最常见的出分方式）
        changed     已有分数被改动
        withdrawn   成绩被撤回，分数栏变回空
        republished 撤回后又原样发布，分数和撤回前一致
    """
    kind: str
    grade: Grade
    old_score: str = ""

    @property
    def label(self) -> str:
        return {
            "new": "新增成绩",
            "filled": "成绩已发布",
            "changed": "成绩变更",
            "withdrawn": "成绩被撤回",
            "republished": "成绩重新发布",
        }.get(self.kind, self.kind)

    @property
    def is_withdrawal(self) -> bool:
        return self.kind == "withdrawn"
