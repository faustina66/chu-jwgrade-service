"""成绩快照的持久化与 diff。"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from .models import Change, Grade, persisted_grade_problem

log = logging.getLogger(__name__)

# 落盘结构的版本。将来改了字段含义就把它加一，老文件会被封存而不是
# 被半懂不懂地读进来——读错比读不出来危险得多。
SNAPSHOT_SCHEMA_VERSION = 1

# 一门课连着这么多轮没抓到，才判定为撤回。默认 1 = 发现就报。
#
# 长安大学的成绩是整行出现、整行消失的：出分时那一行连着分数一起冒出来，
# 撤回时整行不见、后面的行自动补位。所以"行还在但分数栏空了"这种情况根本
# 不存在，唯一的撤回信号就是行消失。
#
# 一开始默认是 2（等一轮再确认），理由是"页面可能返回残缺内容"。后来查了
# 线上日志：52 轮抓取全部 27 门、全部走同一条路径，一次波动都没有——那个
# 担心是凭空想的。而代价是真的：省电档下撤回要等一小时才通知。
#
# 所以默认改成 1，发现就报。真遇到误报（出分季教务处负载最高，那时我们没有
# 数据），把 safety.withdraw_confirm_rounds 调成 2 即可，不用改代码。
#
# 挡残缺页面靠的是另外三道闸，它们和这个数无关：取到 0 条直接报错、
# 整个学期消失要多等一轮、一轮消失达到 max_withdrawals 判定页面异常。
ABSENT_ROUNDS_TO_CONFIRM = 1

# 整个学期都没出现时，改用这个更保守的确认轮数。
#
# 原来是「整学期消失一律不算撤回」。那条规则挡的是页面残缺，但有个盲区：
# **某学期只有一门课时，「这门课被撤回」和「整个学期没抓到」在数据上一模一样。**
# 于是新学期第一门成绩被撤回时没有通知、快照还留着原分数，之后原样重发也
# 认不出来——而那恰好是你最盯着看的那几天。
#
# 现在仍会累计缺失次数，但会多等待一轮再确认。批量消失仍由 max_withdrawals
# 兜住：≥3 门同时消失判页面异常，不推送也不落盘。
TERM_GONE_ROUNDS_TO_CONFIRM = 2


def archive(path: Path, tag: str, why: str) -> Path:
    """使用不会重复的文件名保留状态文件。tag 只由程序内部常量提供。"""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.{tag}-{stamp}-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(path, dest)
    except OSError as e:
        log.error("%s 无法归档（%s）：%s", path, why, e)
        return path
    write_log = log.error if tag == "corrupt" else log.warning
    write_log("%s 已归档（%s）为 %s", path, why, dest.name)
    return dest


def quarantine(path: Path, why: str) -> Path:
    """把读坏了的文件改名封存，而不是删掉或覆盖。

    这类文件里装的是唯一一份状态：快照丢了会重建基线（那一轮的变化就永远
    检不出来），发件箱丢了会直接吞掉一条待发通知。改名的代价只是磁盘上多个
    小文件，而删除是不可逆的——两边不对称到没什么好权衡的。

    改完名下一轮就自愈：文件不存在了，按首次运行/空发件箱走。
    """
    return archive(path, "corrupt", why)


class SnapshotCorrupt(RuntimeError):
    """快照文件读不出来或结构不对。已封存原文件，本轮跳过，下轮自愈。"""


class SnapshotVersionUnsupported(RuntimeError):
    """快照来自未知版本。保留原文件，等待升级程序，绝不重建基线。"""


def account_fingerprint(username: str) -> str:
    """学号的短哈希。只为了认出"这快照不是这个账号的"，不用于任何安全用途。

    存哈希不存学号：快照文件会被 cat、会被贴进聊天框排错。
    """
    u = (username or "").strip()
    return hashlib.sha256(u.encode("utf-8")).hexdigest()[:12] if u else ""


def _snapshot_problem(data: dict) -> str | None:
    """检查快照结构。返回错误说明，None 表示没问题。

    只挡语法错误是不够的：`{"grades": []}` 是合法 JSON，但下一步
    .items() 会抛 AttributeError，而且抛在封存逻辑之外——文件既没被封存，
    程序也没能继续。合法但结构不对的文件比语法坏掉的更需要拦。
    """
    if "grades" not in data:
        return "缺少 grades 字段"
    grades = data["grades"]
    if not isinstance(grades, dict):
        return f"grades 应该是对象，实际是 {type(grades).__name__}"
    if not grades:
        return "grades 是空对象；不存在快照应通过删除文件表示"
    for key, row in grades.items():
        problem = persisted_grade_problem(
            row, f"grades[{key!r}]", storage_key=key)
        if problem:
            return problem
    for field_name in ("adapter", "account"):
        if field_name in data and type(data[field_name]) is not str:
            return f"{field_name} 应该是字符串"
    return None


class GradeStore:
    def __init__(self, path: str | Path, adapter: str = "", username: str = ""):
        self.path = Path(path)
        self.adapter = adapter
        self.account = account_fingerprint(username)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # 坏快照不能当成空快照：那样会静默重建基线，并把原文件覆盖掉。
            # 先封存再抛，本轮记一次失败，下一轮文件已不在，自然按首次运行走。
            quarantine(self.path, str(e))
            raise SnapshotCorrupt(f"快照损坏已封存，本轮跳过：{e}") from e
        if not isinstance(data, dict):
            quarantine(self.path, f"顶层是 {type(data).__name__}")
            raise SnapshotCorrupt("快照顶层不是对象，已封存，本轮跳过")
        if "schema_version" in data:
            version = data["schema_version"]
            if type(version) is not int:
                quarantine(self.path, f"schema_version 应该是整数，实际是 {version!r}")
                raise SnapshotCorrupt("快照版本字段类型不对，已封存，本轮跳过")
            if version != SNAPSHOT_SCHEMA_VERSION:
                raise SnapshotVersionUnsupported(
                    f"快照版本是 {version}，本程序只认 {SNAPSHOT_SCHEMA_VERSION}；"
                    "原文件已保留，请升级程序后再运行")
        problem = _snapshot_problem(data)
        if problem:
            quarantine(self.path, problem)
            raise SnapshotCorrupt(f"快照结构不对，已封存，本轮跳过：{problem}")
        return data

    def _foreign_reason(self, data: dict) -> str | None:
        """检查快照是否属于其他账号；如果是，则返回原因。

        两种情况：
        - 换了适配器。先用 mock 自测再切真实教务系统，快照文件还在但内容
          毫不相干，拿去 diff 会把你全部真实成绩判成"新增"，一次性全推一遍。
        - 换了学号。帮同学查一次、或者自己换了账号，同样会拿 A 的快照去比
          B 的成绩。

        老快照没有 account 字段，此时按"匹配"处理，不然一升级就重建基线。
        """
        recorded = data.get("adapter")
        if self.adapter and recorded and recorded != self.adapter:
            return f"快照来自适配器 {recorded!r}，当前是 {self.adapter!r}"
        acct = data.get("account")
        if self.account and acct and acct != self.account:
            return "快照属于另一个学号"
        return None

    def load(self) -> dict[str, Grade]:
        data = self._read()
        reason = self._foreign_reason(data)
        if reason:
            log.warning("%s，按首次运行重建基线", reason)
            return {}
        return {k: Grade.from_dict(v) for k, v in data.get("grades", {}).items()}

    def _archive_foreign(self, data: dict) -> None:
        """写新基线前保住旧账号快照；归档失败则拒绝覆盖。"""
        reason = self._foreign_reason(data)
        if not reason:
            return
        archived = archive(self.path, "foreign", reason)
        if archived == self.path:
            raise SnapshotCorrupt(
                f"异账号快照归档失败，本轮停止以免覆盖原快照：{reason}")

    def save(self, snapshot: dict[str, Grade]) -> None:
        if self.path.exists():
            self._archive_foreign(self._read())
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "adapter": self.adapter,
            "account": self.account,
            "grades": {k: g.to_dict() for k, g in snapshot.items()},
        }
        # 原子写：先写临时文件再 rename，避免进程被杀时留下半个文件
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @property
    def is_first_run(self) -> bool:
        data = self._read()
        # 这里只判断，不挪文件：新账号还没成功抓到数据时，应让旧账号快照
        # 继续留在原位。真正写入新基线前，save() 才执行原子归档保护。
        return not data or self._foreign_reason(data) is not None


def _terms_seen(new: list[Grade]) -> set[str]:
    return {g.term.strip() for g in new}


def _rounds_needed(confirm_rounds: int, term_gone: bool) -> int:
    """确认一次撤回要连续几轮没抓到。

    整个学期都不见时更谨慎：那**可能**是页面残缺，也**可能**是这学期本来就
    只有一门课、而它被撤回了。分不出来，就多等一轮。
    """
    base = max(1, confirm_rounds)
    return max(base, TERM_GONE_ROUNDS_TO_CONFIRM) if term_gone else base


def _vanished(old: dict[str, Grade], new: list[Grade]):
    """本轮没抓到、但值得当成"可能被撤回"来看的课程。

    产出 (key, 旧记录, 所在学期是否整个消失)。

    一道前置闸：已经确认撤回过的不再重复计数，否则每轮都会重推一次。

    "整个学期没出现"以前是直接不算撤回。那条规则挡住了页面残缺，却也挡住了
    当某学期只有一门课时，两种情况在数据上完全相同。现在统一计数，
    但要求更多确认轮数，由调用方去查 _rounds_needed()。
    """
    present = {g.key for g in new}
    terms = _terms_seen(new)
    for key, prev in old.items():
        if key in present or prev.withdrawn or not prev.has_score:
            continue
        yield key, prev, prev.term.strip() not in terms


def merge(old: dict[str, Grade], new: list[Grade],
          confirm_rounds: int = ABSENT_ROUNDS_TO_CONFIRM) -> dict[str, Grade]:
    """把本轮抓到的成绩并进旧快照。

    用「合并」而不是「替换」：消失的课程记录要留着——确认撤回后它会被打上
    标记继续留在快照里，这样下一轮不会又被当成"新消失的"重推一遍，
    将来它重新出现时也才认得出是重新发布而不是新增。
    """
    merged = dict(old)
    for g in new:
        prev = merged.get(g.key)
        if prev is not None and not g.has_score:
            # 分数栏空了：记住撤回前的分数，重新发布时要用它比对
            g.last_score = prev.score if prev.has_score else prev.last_score
        elif prev is not None and prev.withdrawn:
            # 撤回过又回来了：把撤回前的分数带上，好让 diff 判断改没改
            g.last_score = prev.last_score
        merged[g.key] = g          # 抓到了就重置，absent_rounds 默认为 0

    for key, prev, term_gone in _vanished(old, new):
        rounds = prev.absent_rounds + 1
        if rounds >= _rounds_needed(confirm_rounds, term_gone):
            # 确认撤回：清空分数、记住撤回前的值，并打标记不再重复通知
            merged[key] = dataclasses.replace(
                prev, score="", last_score=prev.score,
                absent_rounds=rounds, withdrawn=True)
        else:
            merged[key] = dataclasses.replace(prev, absent_rounds=rounds)
    return merged


def diff(old: dict[str, Grade], new: list[Grade],
         confirm_rounds: int = ABSENT_ROUNDS_TO_CONFIRM) -> list[Change]:
    """比对新旧成绩。

    两条判定通道，互不干扰：

    1. 按行的存在与否。长安大学的成绩整行出现、整行消失，所以这是撤回的
       唯一信号。见 _vanished() 的两道闸和 ABSENT_ROUNDS_TO_CONFIRM。
    2. 按分数列的变化。有些学校是"课程行早就在，分数栏后来才填上"，
       那种走这条通道。两边共存，换学校时不用改这里。

    全程按「学期::课程代码」比对，和页面上的行顺序无关——出分或撤回会让
    后面的行整体补位，那不是变化。
    """
    changes: list[Change] = []
    for g in new:
        prev = old.get(g.key)

        if prev is None:
            if g.has_score:
                changes.append(Change("new", g))

        elif prev.has_score and not g.has_score:
            changes.append(Change("withdrawn", g, old_score=prev.score))

        elif not prev.has_score and g.has_score:
            if not prev.last_score:
                changes.append(Change("filled", g))
            elif prev.last_score.strip() != g.score.strip():
                # 撤回之后改了分再发 —— 这种最该让人知道
                changes.append(Change("changed", g, old_score=prev.last_score))
            else:
                changes.append(Change("republished", g, old_score=prev.last_score))

        elif prev.has_score and g.has_score and prev.score.strip() != g.score.strip():
            changes.append(Change("changed", g, old_score=prev.score))

    # 通道一：整行从页面上消失
    for _key, prev, term_gone in _vanished(old, new):
        if prev.absent_rounds + 1 >= _rounds_needed(confirm_rounds, term_gone):
            changes.append(Change("withdrawn", prev, old_score=prev.score))

    return changes
