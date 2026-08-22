"""推送发件箱：把"待发送的通知"落盘，保证推送失败的通知不会丢。

只靠"推送失败就不推进快照"是不够的。举个会真实发生的序列：

    第 1 轮  发现高数 92 分，推送失败，快照没保存
    第 2 轮  教务处临时撤回，成绩栏变空
             此时新抓到的空白 vs 旧快照里的空白 = 无变化
             程序保存空白快照，那条 92 分的通知永远消失了

根子在于：待发通知只存在内存里，进程一转身就没了。所以它得落盘——
连同"推送成功后该提交的那份快照"一起，两者必须原子地一起生效。

投递语义是「可能重复，绝不丢失」：推送成功后、清空发件箱前崩溃，
下次会重推一遍。收两条一样的通知，比漏掉一条强得多。

⚠️ 这个"不丢"只保到**提交给推送服务**为止。PushPlus 的 code=200 只表示它
收到了请求，不表示微信真的送到（官方文档明说的）。它接收之后再发失败，本
程序不知道，快照照常推进。详见 notifier.PushPlus.send 的说明。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .models import VALID_CHANGE_KINDS, Change, Grade, persisted_grade_problem
from .store import account_fingerprint, archive, quarantine

log = logging.getLogger(__name__)

OUTBOX_SCHEMA_VERSION = 1


class OutboxCorrupt(RuntimeError):
    """发件箱文件读不出来或结构不对。已封存原文件，本轮跳过，下轮自愈。"""


class OutboxVersionUnsupported(RuntimeError):
    """发件箱来自未知版本。保留原文件，等待升级程序。"""


def _payload_problem(d) -> str | None:
    """检查发件箱内容。返回错误说明，None 表示没问题。

    最该拦的是 `{}`：它是合法 JSON，各个 .get() 也都有默认值，于是会一路
    走到"推送一条空标题空正文的通知，然后把空快照存成正式快照"——
    等于用一份垃圾覆盖掉你的全部成绩记录。
    """
    if not isinstance(d, dict):
        return f"顶层应该是对象，实际是 {type(d).__name__}"

    if type(d.get("title")) is not str or not d["title"].strip():
        return "标题是空的，没有可发的内容"
    if type(d.get("body")) is not str or not d["body"].strip():
        return "正文是空的，没有可发的内容"

    changes = d.get("changes")
    if not isinstance(changes, list):
        return f"changes 应该是数组，实际是 {type(changes).__name__}"
    if not changes:
        return "changes 是空数组，没有对应的成绩变化"
    for i, c in enumerate(changes):
        if not isinstance(c, dict):
            return f"changes[{i}] 应该是对象，实际是 {type(c).__name__}"
        kind = c.get("kind")
        if type(kind) is not str or kind not in VALID_CHANGE_KINDS:
            return f"changes[{i}].kind 不是支持的变化类型：{kind!r}"
        if "old_score" in c and type(c["old_score"]) is not str:
            return f"changes[{i}].old_score 应该是字符串"
        problem = persisted_grade_problem(c.get("grade"), f"changes[{i}].grade")
        if problem:
            return problem

    snapshot = d.get("snapshot")
    if not isinstance(snapshot, dict):
        return f"snapshot 应该是对象，实际是 {type(snapshot).__name__}"
    if not snapshot:
        # 待发通知必然对应一份非空快照。空的只可能是写坏了，
        # 照单收下会把正式快照清成空的。
        return "snapshot 是空的"
    for key, row in snapshot.items():
        problem = persisted_grade_problem(
            row, f"snapshot[{key!r}]", storage_key=key)
        if problem:
            return problem
    for field_name in ("adapter", "account"):
        if field_name in d and type(d[field_name]) is not str:
            return f"{field_name} 应该是字符串"
    return None


class Outbox:
    def __init__(self, path: str | Path, adapter: str = "", username: str = ""):
        self.path = Path(path)
        self.adapter = adapter
        self.account = account_fingerprint(username)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def has_pending(self) -> bool:
        return self.path.exists()

    def stash(self, title: str, body: str, changes: list[Change],
              snapshot: dict[str, Grade]) -> None:
        """把待发通知和它对应的快照一起存下来。"""
        payload = {
            "schema_version": OUTBOX_SCHEMA_VERSION,
            "adapter": self.adapter,
            "account": self.account,
            "title": title,
            "body": body,
            "changes": [{"kind": c.kind, "old_score": c.old_score,
                         "grade": c.grade.to_dict()} for c in changes],
            "snapshot": {k: g.to_dict() for k, g in snapshot.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.info("通知已存入发件箱，下轮会重试推送")

    def load(self) -> tuple[str, str, list[Change], dict[str, Grade]] | None:
        if not self.path.exists():
            return None
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # 直接删掉等于亲手丢一条待发通知，跟这个模块存在的理由正好相反。
            # 封存起来，抛出去让本轮记一次失败；下一轮文件已不在，照常继续。
            quarantine(self.path, str(e))
            raise OutboxCorrupt(f"发件箱损坏已封存，本轮跳过：{e}") from e
        if isinstance(d, dict) and "schema_version" in d:
            version = d["schema_version"]
            if type(version) is not int:
                quarantine(self.path, f"schema_version 应该是整数，实际是 {version!r}")
                raise OutboxCorrupt("发件箱版本字段类型不对，已封存，本轮跳过")
            if version != OUTBOX_SCHEMA_VERSION:
                raise OutboxVersionUnsupported(
                    f"发件箱版本是 {version}，本程序只认 {OUTBOX_SCHEMA_VERSION}；"
                    "原文件已保留，请升级程序后再运行")
        problem = _payload_problem(d)
        if problem:
            quarantine(self.path, problem)
            raise OutboxCorrupt(f"发件箱结构不对，已封存，本轮跳过：{problem}")
        recorded_adapter = d.get("adapter")
        recorded_account = d.get("account")
        mismatch = None
        if self.adapter and recorded_adapter and self.adapter != recorded_adapter:
            mismatch = f"发件箱来自适配器 {recorded_adapter!r}，当前是 {self.adapter!r}"
        elif self.account and recorded_account and self.account != recorded_account:
            mismatch = "发件箱属于另一个学号"
        if mismatch:
            archived = archive(self.path, "foreign", mismatch)
            if archived == self.path:
                raise OutboxCorrupt(
                    f"异账号发件箱归档失败，本轮停止以免覆盖原通知：{mismatch}")
            return None
        changes = [Change(c["kind"], Grade.from_dict(c["grade"]), c.get("old_score", ""))
                   for c in d.get("changes", [])]
        snapshot = {k: Grade.from_dict(v) for k, v in d.get("snapshot", {}).items()}
        return d.get("title", ""), d.get("body", ""), changes, snapshot

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
