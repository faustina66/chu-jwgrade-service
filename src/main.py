"""入口：定时查成绩，有变化就推送微信。

    python -m src.main               # 常驻轮询（自适应频率）
    python -m src.main --once        # 只跑一次（配合 cron / 计划任务）
    python -m src.main --history 20  # 看最近 20 条成绩变更历史
    python -m src.main --test-notify # 只测推送通道
    python -m src.main --unlock-login # 确认新密码后解除登录阻断
    python -m src.main --once --dump # 保存成绩页原始 HTML，用于调试解析
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from . import adapters, config, credentials, heartbeat, history, notifier, outbox
from .adapters import LoginFailed, LoginNeedsHuman
from .instance_lock import InstanceAlreadyRunning, InstanceLock
from .loginblock import LoginBlock, LoginBlocked, LoginBlockError
from .loginrate import (
    PASSWORD,
    TICKET,
    LoginRate,
    LoginRateLimited,
    LoginRateStateError,
)
from .models import Change, Grade
from .redact import redact_url
from .scheduler import AdaptiveScheduler, ScheduleConfig
from .session_store import SessionStore, SessionStoreError
from .store import GradeStore, SnapshotVersionUnsupported, diff, merge

log = logging.getLogger("jw")


@dataclass
class CheckResult:
    """一轮检查的结果。

    光返回 changes 不够：推送失败时它同样是一串变化，调用方分不出
    "这轮很顺利" 和 "变化检出来了但一条都没发出去"。后者必须计入连续失败，
    否则推送通道坏掉之后，日志上永远是一片岁月静好。
    """
    changes: list
    delivered: bool = True
    total: int = 0            # 本轮实际抓到多少门课，报平安时用得上

# 退出码。systemd 靠它区分"重试有意义"和"重试只会更糟"：
#   20 登录失败（密码错、账号锁定、要验证码）—— 再试只会把账号试死
#   21 配置或凭据缺失 —— 人不改配置，重启一万次也一样
EXIT_LOGIN_FAILED = 20
EXIT_CONFIG_ERROR = 21


def demo_grade() -> Grade:
    """--demo 用的假成绩。**字段要和真实抓到的一模一样**，缺一格 demo 就
    和线上长得不一样了——2026-08-19 就漏过 `课程序号`，推出来少一行。

    分数一律满分，不放真实成绩：这条消息会被截图、会贴给别人看。

    kind 固定用 new 而不是 filled：长安大学的成绩是整行出现、整行消失的，
    不存在「课程行早就在、分数栏后来才填上」那个中间状态，filled 在这儿
    永远不会发生。演示数据要演示真会发生的事。
    """
    term = "2025-2026 2"
    return Grade(
        term=term, course_id="12XK1102", course_name="高等数学Ⅱ（二）",
        score="100", credit="5", gpa="5",
        raw={"学年学期": term, "课程代码": "12XK1102", "课程序号": "12XK1102.03",
             "课程名称": "高等数学Ⅱ（二）", "课程类别": "学科基础课程（2022）",
             "学分": "5", "期中成绩": "100", "期末成绩": "100",
             "平时成绩": "100", "总评成绩": "100", "实验成绩": "100",
             "最终": "100", "绩点": "5"})


def _setup_logging(config_path: str = "config.yaml") -> None:
    # 日志跟着配置文件走，不跟当前工作目录走。状态文件早就是这个规矩了
    # （见 config._anchor_storage_paths）；日志要是按 cwd 解析，从别的目录
    # 手工跑一次命令就会在那边另建一份 data/run.log，日志散成好几份，
    # 排查时看的和程序写的不是同一个文件。
    #
    # FileHandler 不会自己建目录。全新安装时 data/ 还不存在，
    # 于是程序在做任何一件正事之前就先抛 FileNotFoundError——
    # 而且是在日志系统里抛的，连报错都记不下来。
    data_dir = Path(config_path).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # 日志里有课程数、错误详情和偶尔的页面片段。同样不指望 UMask：
    # 先按 0600 把文件建出来，FileHandler 再以追加方式接上。
    log_path = data_dir / "run.log"
    if not log_path.exists():
        os.close(os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600))

    # Windows 控制台默认 GBK，日志里的中文和 emoji 会直接抛 UnicodeEncodeError。
    # errors="replace" 保证再冷门的字符也只是显示成问号，不会中断程序。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def _in_quiet_hours(window) -> bool:
    if not window or len(window) != 2:
        return False
    start, end = window
    hour = dt.datetime.now().hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


# 存活计时器。装在模块级而不是层层传参：每一条成功送达的通知都该把它归零，
# 而推送出口散落在 check_once、发件箱补发、失败告警、--report 好几处。
# 之前只有"本轮有成绩变化"和"心跳自己"会归零，于是出现过发件箱刚补发一条
# 成绩通知、紧接着又推一条"监控正常"的场面。
_heartbeat = None


def _push(notifiers: list, title: str, body: str) -> bool:
    """返回是否全部送达。调用方靠它决定要不要推进快照。"""
    if not notifiers:
        log.warning("没有启用任何推送通道，通知内容只写进日志：%s%s", chr(10), body)
        return True          # 没通道就没什么可重试的，别把快照永远卡住

    def one(n) -> bool:
        # 一个通道抛异常不该带崩整轮：它会一路窜到 check_once 外面，
        # 让本来已经检出的变化连发件箱都进不去。
        try:
            return n.send(title, body)
        except Exception as e:
            log.exception("[%s] 推送时抛异常：%s", getattr(n, "name", "?"), e)
            return False

    # 列表推导是有意的，别改成生成器：all() 会短路，那样第一个通道失败之后
    # 后面的通道根本不会被调用。
    ok = all([one(n) for n in notifiers])    # noqa: C419
    if ok and _heartbeat is not None:
        _heartbeat.note_push()
    return ok


def _push_critical(notifiers: list, title: str, body: str,
                   attempts: int | None = None, delay: int = 60) -> bool:
    """关键通知：发不出去就退避重试。

    "监控已停止"是全部通知里最不能丢的一条：登录失败会退 20，而 systemd 的
    RestartPreventExitStatus 认这个码，服务不会再被拉起来。这条消息没送到，
    监控就永久死在那儿，而你看到的现象是"最近没出分"——和一切正常一模一样。

    PushPlus 免费额度 200 条/天，用完了正好就是这个下场，并不罕见。
    """
    i = 0
    while attempts is None or i < attempts:
        i += 1
        if _push(notifiers, title, body):
            return True
        if attempts is not None and i >= attempts:
            break
        suffix = f"/{attempts}" if attempts is not None else ""
        log.error("关键通知未送达（第 %d%s 次），%d 秒后重试",
                  i, suffix, delay)
        time.sleep(delay)
        delay = min(delay * 2, 3600)
    log.error("关键通知始终没能送出。监控即将停止，而你不会收到任何提示——"
              "请自行确认服务状态：systemctl status jwgrade")
    return False


def _persist_login_failure(block: LoginBlock, auth_required: bool,
                           password: str, reason: str,
                           notifiers: list, *,
                           needs_human: bool = False) -> str:
    """落盘登录失败；连安全标记都写不进去时，先发安全故障告警再停机。

    needs_human 区分的是**恢复方式**，不是严重程度：验证码和账号临时锁定
    都可能发生在密码完全正确时，那种标记不该要求换密码才能解锁。
    """
    safe_reason = redact_url(reason) or reason
    if not auth_required:
        return safe_reason
    try:
        block.block(password, safe_reason, needs_human=needs_human)
    except LoginBlockError as error:
        log.critical("登录阻断标记写入失败，已拒绝继续运行：%s", error)
        _push_critical(
            notifiers,
            "⚠️ 教务监控安全故障",
            f"登录失败但无法写入阻断标记，程序已停止。\n\n"
            f"登录原因：{safe_reason}\n阻断标记错误：{error}",
            attempts=None,
        )
        raise
    return safe_reason


def _flush_outbox(box, store: GradeStore, notifiers: list,
                  hist: history.History | None) -> bool:
    """先把上次没推出去的通知补发掉。返回发件箱是否已清空。"""
    pending = box.load() if box else None
    if pending is None:
        return True
    title, body, changes, snapshot = pending
    log.info("发件箱里有上次没推出去的通知，先补发")
    if not _push(notifiers, title, body):
        log.error("补发仍然失败，本轮跳过抓取，下轮再试")
        return False
    if hist:
        hist.append(changes)
    store.save(snapshot)      # 通知和它对应的快照必须一起生效
    box.clear()
    log.info("补发成功，发件箱已清空")
    return True


def check_once(adapter, store: GradeStore, notifiers: list,
               hist: history.History | None = None,
               max_withdrawals: int = 3,
               level: str = "full", box=None, login_hooks=None,
               confirm_rounds: int = 1) -> CheckResult:
    """查一轮。"""
    if not _flush_outbox(box, store, notifiers, hist):
        # 发件箱没清空就不抓新数据：通知得按顺序发，而且这轮注定还是发不出去
        return CheckResult([], delivered=False)

    first_run = store.is_first_run
    old = store.load()
    if login_hooks is None:
        grades = adapter.run()
    else:
        grades = adapter.run(
            on_login_start=login_hooks.get("on_login_start"),
            on_login_success=login_hooks.get("on_login_success"),
            on_login_failure=login_hooks.get("on_login_failure"),
            on_password_submit=login_hooks.get("on_password_submit"),
            on_password_gate=login_hooks.get("on_password_gate"),
            on_ticket_start=login_hooks.get("on_ticket_start"),
            on_ticket_success=login_hooks.get("on_ticket_success"),
        )
    if not grades:
        raise RuntimeError("取到 0 条成绩，判定为异常，本轮不覆盖快照")

    changes = diff(old, grades, confirm_rounds)

    # 一次撤回一堆课，现实中几乎不可能，多半是教务处返回了残缺页面。
    # 这里必须赶在 save 之前拦下：一旦写进快照，等页面恢复正常就会被
    # 当成"批量出分"再全推一遍。
    withdrawals = [c for c in changes if c.is_withdrawal]
    if len(withdrawals) >= max_withdrawals:
        names = "、".join(c.grade.course_name for c in withdrawals[:5])
        raise RuntimeError(
            f"本轮 {len(withdrawals)} 门成绩同时消失（{names}…），判定为页面异常，"
            f"跳过本轮且不更新快照"
        )

    if first_run:
        # 首次运行时全部课程都是"新"的，推送出去等于刷屏。只建基线。
        store.save(merge(old, grades, confirm_rounds))
        with_score = sum(1 for g in grades if g.has_score)
        log.info("首次运行，已建立基线：%d 门课程（其中 %d 门已出分），本次不推送",
                 len(grades), with_score)
        return CheckResult([], total=len(grades))

    if not changes:
        store.save(merge(old, grades, confirm_rounds))
        log.info("无变化（共 %d 门课程）", len(grades))
        return CheckResult([], total=len(grades))

    log.info("检测到 %d 项变化", len(changes))
    title, body = notifier.render(changes, level)
    merged = merge(old, grades, confirm_rounds)
    if not _push(notifiers, title, body):
        # 推送失败：把通知连同它对应的快照一起存进发件箱，下轮补发。
        # 只是"不推进快照"不够——万一下一轮教务处又把成绩撤回成空白，
        # 新旧一比又是"无变化"，这条通知就永远消失了。
        log.error("推送失败，通知转入发件箱")
        if box:
            box.stash(title, body, changes, merged)
        return CheckResult(changes, delivered=False, total=len(grades))

    if hist:
        hist.append(changes)
    store.save(merged)
    return CheckResult(changes, total=len(grades))


def _maybe_heartbeat(beat, notifiers: list, res: CheckResult) -> None:
    """长时间没给你发过任何东西时，主动报个平安。

    有真实变化就只是把计时器归零：出分季本来消息就够多了，
    再加一条"我还活着"纯属噪音。
    """
    # 有变化时不用在这儿归零：check_once 里那次 _push 成功后已经归零了。
    if res.changes or not beat.due():
        return
    body = (f"监控正常运行中，已盯住 {res.total} 门课程；"
            f"最近 {beat.days} 天没有新成绩。"
            "收到这条说明服务活着——没收到才该去看一眼。")
    _push(notifiers, "✅ 教务监控正常", body)     # 送达则由 _push 自己归零


def _load_schedule(cfg: dict, override_interval: int = 0) -> ScheduleConfig:
    sched = ScheduleConfig.from_dict(cfg.get("schedule", {}))
    if override_interval:
        # 命令行显式指定间隔时，关掉自适应，否则两者语义打架
        sched.adaptive = False
        sched.interval = override_interval
    return sched


def main() -> int:
    """最外层退出码映射。

    分三类，systemd 靠它决定要不要拉起来重试：
      EXIT_LOGIN_FAILED  密码错、账号锁定、要验证码 —— 重试只会把账号试死
      EXIT_CONFIG_ERROR  配置或凭据缺失 —— 人不改，重启一万次也一样
      1 / 其它           网络抖动之类的临时故障 —— 重试有意义

    必须在这里统一收口：--once 模式下 LoginFailed 会一路抛到顶层，
    只在常驻循环里 return 20 是不够的。
    """
    try:
        return _run()
    except LoginFailed as e:
        log.error("登录失败，停止运行：%s", e)
        return EXIT_LOGIN_FAILED
    except (config.ConfigError, FileNotFoundError, SnapshotVersionUnsupported,
            outbox.OutboxVersionUnsupported, LoginBlockError,
            LoginRateStateError, SessionStoreError) as e:
        # 配置问题不是程序崩溃，别拿 traceback 糊用户一脸
        print()
        print(e)
        print()
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        log.info("收到中断，退出")
        return 0


def _run() -> int:
    ap = argparse.ArgumentParser(description="教务成绩自动监控")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="只查一次就退出")
    ap.add_argument("--interval", type=int, default=0,
                    help="固定轮询间隔（秒），覆盖配置并关闭自适应")
    ap.add_argument("--history", nargs="?", type=int, const=0, default=None,
                    metavar="N", help="查看成绩变更历史，可指定只看最近 N 条")
    ap.add_argument("--test-notify", action="store_true", help="只发一条测试推送")
    ap.add_argument("--report", action="store_true",
                    help="把当前快照里的完整成绩单推到微信")
    ap.add_argument("--demo", action="store_true",
                    help="用假数据模拟一次出分通知，看当前 detail_level 的实际效果")
    ap.add_argument("--dump", action="store_true", help="保存成绩页原始 HTML")
    ap.add_argument("--set-password", action="store_true", help="把密码存进系统密钥链")
    ap.add_argument("--clear-password", action="store_true", help="从密钥链删除密码")
    ap.add_argument("--unlock-login", action="store_true",
                    help="确认已更换正确密码后解除登录阻断")
    ap.add_argument("--preflight", action="store_true",
                    help="只检查登录状态目录，不访问教务系统")
    args = ap.parse_args()

    _setup_logging(args.config)
    # 只读命令不要求推送配置齐全：它们既不推送，也常常在没有
    # /etc/jwgrade.env 的情况下手工执行。
    readonly = (args.history is not None or args.set_password or args.clear_password
                or args.unlock_login or args.preflight)
    cfg = config.load(args.config, require_push=not readonly)
    username = cfg["account"].get("username", "")
    adapter_name = cfg["account"].get("adapter", "mock")
    storage = cfg.get("storage", {})

    # --interval 在 _load_schedule 里是直接改字段的，配置那道下限校验管不到它。
    # 不在这儿拦的话，`--interval 30` 就是每分钟打一次学校，而唯一的兜底是
    # next_delay() 里的 max(60, ...)。两条路共用 config.interval_problem。
    if args.interval:
        too_dense = config.interval_problem(
            "--interval", args.interval,
            (cfg.get("schedule") or {}).get("quiet_hours") or ())
        if too_dense:
            print(f"{chr(10)}{too_dense}{chr(10)}")
            return EXIT_CONFIG_ERROR

    if args.preflight:
        try:
            rate_path, session_path = config.validate_runtime_storage(cfg)
        except config.ConfigError as e:
            print(f"\n{e}\n")
            return EXIT_CONFIG_ERROR
        print(f"状态目录检查通过：{rate_path.parent}")
        print(f"登录限速文件：{rate_path}")
        print(f"登录会话文件：{session_path}")
        # 秒数没人判断得了，轮数可以。改完频率随手跑一下这个就知道调成了什么。
        sc = ScheduleConfig.from_dict(cfg.get("schedule") or {})
        print()
        if sc.adaptive:
            print(f"轮询节奏：加速 {sc.active_interval // 60} 分钟 / "
                  f"常规 {sc.interval // 60} 分钟 / "
                  f"省电 {sc.idle_interval // 60} 分钟")
        else:
            print(f"轮询节奏：固定 {sc.interval // 60} 分钟（adaptive: false）")
        if len(sc.quiet_hours) == 2:
            print(f"静默时段：{sc.quiet_hours[0]:02d}:00–"
                  f"{sc.quiet_hours[1]:02d}:00 不查")
        print(f"每天约 {config.rounds_per_day(sc.interval, sc.quiet_hours)} 轮"
              f"（常规档）／"
              f"{config.rounds_per_day(sc.active_interval, sc.quiet_hours)} 轮"
              f"（加速档）")
        return 0

    if args.history is not None:
        hist = history.History(storage.get("history_path", "data/history.jsonl"))
        print(history.render(hist.read(args.history)))
        return 0
    if args.set_password:
        return credentials.store_interactive(username)
    if args.clear_password:
        return credentials.clear(username)
    if args.unlock_login:
        if adapter_name == "mock":
            print("mock 适配器不需要解除登录阻断。")
            return 0
        try:
            password, source = credentials.resolve(
                username, cfg["account"].get("password", ""))
        except ValueError as e:
            print(f"\n{e}\n")
            return EXIT_CONFIG_ERROR
        log.info("密码来源：%s", source)
        block = LoginBlock(storage.get("block_path") or "data/login_blocked")
        try:
            block.unlock(password)
        except LoginBlockError as e:
            print(f"\n{e}\n")
            return EXIT_CONFIG_ERROR
        print("已显式解除登录阻断。下一次监控才会尝试登录。")
        return 0

    notifiers = notifier.build(cfg.get("notify", {}))
    # 手动的 --test-notify / --demo / --report 也会走 _push；把同一份
    # 存活计时器接上，避免成功送达后仍被误判为长期无推送。
    global _heartbeat
    _heartbeat = heartbeat.Heartbeat(
        storage.get("heartbeat_path") or "data/last_push.marker",
        cfg.get("notify", {}).get("heartbeat_days", 7),
    )

    if args.test_notify:
        if not notifiers:
            print("\n没有启用任何推送通道，成绩变化只会写进日志和历史，不会推到微信。\n"
                  "  1. 去 https://www.pushplus.plus 微信扫码登录，复制首页的 token\n"
                  "  2. 写进环境变量 PUSHPLUS_TOKEN，或填进 config.yaml 的 notify.pushplus.token\n")
            return EXIT_CONFIG_ERROR
        log.info("已启用通道: %s", [n.name for n in notifiers])
        # 推送失败要以非零退出，否则脚本里 `--test-notify && echo ok` 会误报成功
        return 0 if _push(notifiers, "✅ 教务监控测试", "推送通道正常，可以开始监控了。") else 1

    if args.demo:
        # 不碰快照、不碰教务系统，纯粹让你看清当前配置推出来长什么样。
        level = cfg.get("notify", {}).get("detail_level", "full")
        title, body = notifier.render([Change("new", demo_grade())], level)
        print()
        print(f"详略级别: {level}")
        print(f"标题: {title}")
        print("正文纯文本:")
        print(BeautifulSoup(body, "lxml").get_text(chr(10), strip=True))
        print()
        return 0 if _push(notifiers, title, body) else 1

    if args.report:
        # 读快照而不是重新抓取：不碰教务系统，服务停着也能用
        snapshot = GradeStore(
            storage.get("snapshot_path", "data/grades.json"),
            adapter_name, username).load()
        if not snapshot:
            print("\n快照是空的，先跑一次 --once 建立基线。\n")
            return EXIT_CONFIG_ERROR
        msgs = notifier.render_report(list(snapshot.values()))
        ok = True
        for title, body in msgs:
            # 正文是 HTML，原样打到终端就是一屏源码。剥成纯文本给人看。
            print(f"\n{title}\n")
            print(BeautifulSoup(body, "lxml").get_text("\n", strip=True))
            print(f"\n（HTML 正文 {len(body)} 字符）")
            ok = _push(notifiers, title, body) and ok
        if len(msgs) > 1:
            log.info("成绩单较长，已按学期拆成 %d 条推送", len(msgs))
        return 0 if ok else 1

    # --once 和常驻模式都会写快照/发件箱，因此共用同一把 OS 锁。命令模式
    # 已在上面返回，不会因为后台监控正在运行而影响查历史、改密码或发测试。
    run_lock = InstanceLock(storage.get("lock_path", "data/jwgrade.lock"))
    try:
        run_lock.acquire()
    except InstanceAlreadyRunning as e:
        log.error("%s", e)
        return 1
    try:
        return _run_monitor(args, cfg, username, storage, notifiers)
    finally:
        run_lock.release()


def _run_monitor(args, cfg: dict, username: str, storage: dict,
                 notifiers: list) -> int:
    """只在持有单实例锁时进入的抓取/写状态路径。"""
    adapter_name = cfg["account"].get("adapter", "mock")
    hist = history.History(storage.get("history_path", "data/history.jsonl"))
    box = outbox.Outbox(storage.get("outbox_path", "data/pending.json"),
                        adapter_name, username)
    global _heartbeat
    block = LoginBlock(storage.get("block_path") or "data/login_blocked")
    beat = heartbeat.Heartbeat(
        storage.get("heartbeat_path") or "data/last_push.marker",
        cfg.get("notify", {}).get("heartbeat_days", 7))
    _heartbeat = beat

    acct = dict(cfg["account"])
    acct["debug_dump"] = args.dump
    # 适配器自己要落盘的东西（--dump 的原始页面、mock 的样例数据）都放这儿，
    # 不按当前工作目录解析。dump 里有姓名、学号和全部成绩，落点必须是确定的：
    # 否则从别处手工跑一次，这份东西就悄悄留在那个目录，你既不知道它在哪，
    # 也不会想起来删；mock 那个更直接，会在你所在的任意目录 mkdir 出一个 data/。
    acct["state_dir"] = str(
        Path(storage.get("snapshot_path") or "data/grades.json").parent)
    auth_required = acct.get("adapter") != "mock"
    if auth_required:
        config.validate_runtime_storage(cfg)
        try:
            acct["password"], source = credentials.resolve(username, acct.get("password", ""))
        except ValueError as e:
            # 配置问题不是程序崩溃，别拿 traceback 糊用户一脸
            print(f"\n{e}\n")
            return EXIT_CONFIG_ERROR
        log.info("密码来源：%s", source)
        # 密码错了就别再试。cron 和 Windows 任务计划程序不看退出码，
        # 每 15 分钟拿错密码撞一次，一天 96 次，够把统一身份认证锁死。
        try:
            block.check(acct["password"])
        except LoginBlocked as e:
            # 服务重启/机器重启后也必须把“监控已停止”补发出去；阻断标记
            # 证明上次已经发生过登录失败，不能只静默返回 21。
            log.error("登录仍处于阻断状态：%s", e)
            _push_critical(notifiers, "⚠️ 教务监控已停止",
                           f"登录仍被阻断，需要处理后才能恢复：\n\n{e}",
                           attempts=None)
            return EXIT_CONFIG_ERROR
    adapter_name = acct.get("adapter", "mock")
    adapter = adapters.get(adapter_name, acct)
    # 会话落盘：重启不该等于一次登录。cookie 原本只活在进程内存里，
    # 每次 systemctl restart 都会丢掉长期票据、逼出一次完整登录——
    # 而完整登录被限成一天一次之后，那就意味着当天第二次重启直接瘫痪。
    sessions = SessionStore(storage.get("session_path") or "data/session.json",
                            username)
    # 用 getattr 而不是直接取：适配器不一定基于 requests.Session
    # （将来换个学校可能是别的实现），会话落盘只是优化，不该成为硬要求。
    adapter_session = getattr(adapter, "session", None)
    if auth_required and adapter_session is not None:
        restored = sessions.restore(adapter_session)
        if restored and adapter.resume_from_cookies():
            # 教务系统的会话看着还活着，直接跳过登录，让第一次抓取去验证。
            # 不这么做的话，重启仍然要走一遍换票——而完整登录一天只有一次
            # 额度，白白消耗掉的话当天再重启就登不进去了。
            log.info("已恢复上次的登录会话（%d 条 cookie），本轮不重新登录",
                     restored)
        elif restored:
            log.info("已恢复 %d 条 cookie，但教务系统会话已失效，需要换票",
                     restored)
    store = GradeStore(storage.get("snapshot_path", "data/grades.json"),
                       adapter_name, username)
    max_withdrawals = int(cfg.get("safety", {}).get("max_withdrawals", 3))
    confirm_rounds = int(cfg.get("safety", {}).get("withdraw_confirm_rounds", 1))
    level = cfg.get("notify", {}).get("detail_level", "full")
    # 登录频率硬闸分成两处：换票请求和真正提交密码请求。两者都必须在
    # 适配器发出请求前先过小时爆发限制，密码提交还要过 24 小时总量限制。
    rate = LoginRate(storage.get("rate_path") or "data/login_rate.json",
                     int(cfg.get("safety", {}).get("max_logins_per_hour", 1)),
                     int(cfg.get("safety", {})
                         .get("max_password_logins_per_day", 1)))

    def _save_session() -> None:
        if auth_required and adapter_session is not None:
            sessions.save(adapter_session)

    def _before_ticket() -> None:
        # 每一轮认证都从换票探测开始，所以小时爆发闸挂在这儿，一轮记一次。
        # 记账在请求发出之前：换票失败照样是一次打到认证服务器的请求，
        # 只在成功时记会漏掉恰恰最该数的那些。
        rate.check_attempt()
        rate.note(TICKET)

    def _gate_password() -> None:
        # 只查，不记账。这里不查小时闸：换票和随后的密码提交是同一轮认证的
        # 两个阶段，换票那步已经记过一次，再查会把自己拦死。
        rate.check_password()

    def _before_password() -> None:
        # 真的要把密码发出去了才记账。放在请求之前而不是之后：
        # 进程崩在半路时，那次尝试对认证服务器来说已经发生了。
        block.arm(acct["password"])
        rate.note(PASSWORD)

    def _on_login_success() -> None:
        # 会话先保存成功，再解除登录阻断。若保存失败，阻断标记必须留下，
        # 这样人工重启也不会在没有会话的情况下再次进入认证流程。
        _save_session()
        block.clear()

    login_hooks = ({
        # 不传 on_login_failure：阻断标记只在真正成功后清除。密码提交后发生
        # 网络异常时，必须保留标记，避免下一轮再次提交同一密码。
        "on_login_success": _on_login_success,
        "on_password_gate": _gate_password,
        "on_password_submit": _before_password,
        "on_ticket_start": _before_ticket,
    } if auth_required else None)

    if args.once:
        try:
            res = check_once(adapter, store, notifiers, hist,
                             max_withdrawals, level, box, login_hooks,
                             confirm_rounds)
        except LoginBlockError as e:
            # 同上：--once 常挂在 cron 上，退出码没人看，不推就等于没发生
            _push_critical(notifiers, "⚠️ 教务监控已停止",
                           f"登录阻断标记异常，已停机等待人工处理：{chr(10)}{chr(10)}{e}",
                           attempts=3)
            raise
        except (LoginRateStateError, SessionStoreError) as e:
            _push_critical(notifiers, "⚠️ 教务监控安全故障",
                           f"登录/会话安全状态无法安全读写，程序已停止：\n\n{e}",
                           attempts=3)
            raise
        except LoginRateLimited as e:
            log.warning("本轮因登录频率限制跳过：%s", e)
            return 1
        except LoginFailed as e:
            # 常驻模式在循环里处理；--once 以前是一路抛到 main() 只记个日志，
            # 既不告警也不阻止下一次定时任务继续拿错密码去撞。
            log.error("登录失败，停止运行：%s", e)
            safe_reason = _persist_login_failure(
                block, auth_required, acct.get("password", ""), str(e), notifiers,
                needs_human=isinstance(e, LoginNeedsHuman))
            _push_critical(notifiers, "⚠️ 教务监控已停止",
                           f"登录失败，已阻断后续尝试，需要你处理：{safe_reason}",
                           attempts=None)
            return EXIT_LOGIN_FAILED
        if auth_required:
            block.clear()
        # 推送没送达就以非零退出，否则 cron 里 `--once && echo ok` 会报喜不报忧。
        # 返回 1 而不是 20/21：这是临时故障，下次重试有意义。
        return 0 if res.delivered else 1

    sched = _load_schedule(cfg, args.interval)
    scheduler = AdaptiveScheduler(sched)
    cfg_mtime = os.path.getmtime(args.config)

    log.info("开始监控：%s，静默时段 %s", sched.describe(), sched.quiet_hours or "无")
    log.info("想改频率直接编辑 %s，无需重启", args.config)
    fails = 0
    alerted = False
    while True:
        try:
            if _in_quiet_hours(scheduler.cfg.quiet_hours):
                log.info("静默时段，跳过本轮")
            else:
                res = check_once(adapter, store, notifiers, hist,
                                 max_withdrawals, level, box, login_hooks,
                                 confirm_rounds)
                if not res.delivered:
                    # 通知躺在发件箱里没发出去，这不算一轮成功。
                    # 不发告警推送：推送通道本身就是坏的，告警也一样发不出去。
                    fails += 1
                    log.warning("第 %d 次失败：通知未送达，已进发件箱等待补发", fails)
                    # note(True) 让调度器进加速档尽快重试。这不会多打教务系统——
                    # 发件箱没清空前 check_once 根本不抓取。
                    scheduler.note(True)
                else:
                    if auth_required:
                        block.clear()      # 登得进去，说明凭据是好的
                    _maybe_heartbeat(beat, notifiers, res)
                    scheduler.note(bool(res.changes))
                    if fails:
                        log.info("已从连续 %d 次失败中恢复", fails)
                    fails, alerted = 0, False
        except (SnapshotVersionUnsupported, outbox.OutboxVersionUnsupported):
            # 新版本状态不能当临时网络错误反复重试。让异常到最外层映射成 21，
            # systemd 会按 RestartPreventExitStatus 停止，等待程序升级。
            raise
        except LoginFailed as e:
            # 密码错误/账号锁定重试也没用，直接告警退出，免得把账号试锁死。
            # 退出码必须是 EXIT_LOGIN_FAILED：systemd 的 RestartPreventExitStatus
            # 认的就是它，返回 1 的话服务照样被拉起来重试，等于没防。
            log.error("登录失败，停止运行：%s", e)
            safe_reason = _persist_login_failure(
                block, auth_required, acct.get("password", ""), str(e), notifiers,
                needs_human=isinstance(e, LoginNeedsHuman))
            _push_critical(notifiers, "⚠️ 教务监控已停止",
                           f"登录失败，需要你处理：\n\n{safe_reason}", attempts=None)
            return EXIT_LOGIN_FAILED
        except LoginRateLimited as e:
            # 不是故障，是闸门在起作用。仍然计入连续失败：万一它持续拦着
            # （比如会话每几分钟就失效），第 5 次会推一条告警给你，
            # 而不是让监控无声无息地停摆。
            fails += 1
            log.warning("第 %d 次跳过：%s", fails, e)
            if fails >= scheduler.cfg.fail_alert_after and not alerted:
                alerted = _push(
                    notifiers, "⚠️ 教务监控登录受限",
                    f"已连续 {fails} 轮因登录过于频繁而跳过。{e}")
        except (LoginBlockError, LoginRateStateError, SessionStoreError) as e:
            # 阻断标记是安全边界，读写失败时宁可停机，也不能退回到
            # 无保护的登录重试循环。但停机必须让人知道：退 21 之后
            # systemd 不再拉起，不推这一条就是"安静地死掉"——
            # 而那正是心跳和关键通知要防的东西。
            _push_critical(notifiers, "⚠️ 教务监控已停止",
                           f"登录/会话安全状态异常，已停机等待人工处理："
                           f"{chr(10)}{chr(10)}{e}",
                           attempts=3)
            raise
        except KeyboardInterrupt:
            log.info("收到中断，退出")
            return 0
        except Exception as e:
            fails += 1
            log.warning("第 %d 次失败：%s", fails, e, exc_info=fails == 1)
            if fails >= scheduler.cfg.fail_alert_after and not alerted:
                # alerted 只在真送到了才置位。否则推送恰好也坏着的那一次，
                # 这条告警就被永久跳过——而推送坏掉本身就是该告警的原因之一。
                alerted = _push(
                    notifiers, "⚠️ 教务监控连续失败",
                    f"已连续失败 {fails} 次，程序仍在重试。\n\n最后一次错误：\n{e}")

        cfg_mtime = _maybe_reload(args, scheduler, cfg_mtime)
        delay, label = scheduler.next_delay()
        log.info("下次检查：%d 秒后（%s）", delay, label)
        time.sleep(delay)


def _maybe_reload(args, scheduler: AdaptiveScheduler, mtime: float) -> float:
    """配置文件改了就热加载**轮询频率**，省得为改个间隔重启进程。

    只覆盖 schedule 这一段。safety（登录闸）、notify（详略级别）、storage
    （状态文件路径）都要重启才生效——它们在启动时就被读进了局部变量和
    已经构造好的对象里。日志措辞必须说清楚这一点：有人为了保险把
    max_logins_per_hour 改小，看见「配置已重载」就以为生效了，那是对
    安全设置的错误信心。
    """
    try:
        current = os.path.getmtime(args.config)
    except OSError:
        return mtime
    if current == mtime:
        return mtime
    try:
        sched = _load_schedule(config.load(args.config), args.interval)
    except Exception as e:    # noqa: BLE001 —— 配置怎么写坏的都不该让监控停摆
        # 多半是编辑到一半，YAML 还不完整。沿用旧配置，并记下这个 mtime——
        # 不是"下轮再试"：那样会每一轮都重新解析同一份坏文件、刷一遍同样的
        # 警告。你把它改对时 mtime 会再变一次，那时自然会重新加载。
        log.warning("配置重载失败，继续使用原配置：%s", e)
        return current
    scheduler.reload(sched)
    log.info("轮询频率已重载：%s，静默时段 %s"
             "（热加载只覆盖 schedule；safety / notify / storage 的改动要重启才生效）",
             sched.describe(), sched.quiet_hours or "无")
    return current


if __name__ == "__main__":
    sys.exit(main())
