"""配置加载。密码和推送 key 支持走环境变量，避免明文落在服务器磁盘上。"""
from __future__ import annotations

import logging
import os
from itertools import pairwise
from pathlib import Path

import yaml

from .adapters import available_names
from .scheduler import ScheduleConfig

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """配置本身有问题，重启多少次都一样——调用方据此返回 EXIT_CONFIG_ERROR。"""


# 顶层各段都必须是映射。填成列表或标量时如果不拦，后面 .get() 会抛
# AttributeError，用户看到的是一串 traceback 而不是"你第几行写错了"。
_SECTIONS = ("account", "schedule", "notify", "safety", "storage")
_DEPRECATED_SECTIONS = {"web"}
_KEYS = {
    "account": {"adapter", "username", "password", "service_url",
                 "semester_id", "base_url", "debug_dump"},
    "schedule": {"adaptive", "interval_seconds", "active_interval_seconds",
                  "idle_interval_seconds", "active_duration_minutes",
                  "idle_after_hours", "jitter_seconds", "quiet_hours",
                  "fail_alert_after"},
    "notify": {"heartbeat_days", "detail_level", "pushplus"},
    "notify.pushplus": {"enabled", "token"},
    "safety": {"max_withdrawals", "max_logins_per_hour",
               "max_password_logins_per_day", "withdraw_confirm_rounds"},
    "storage": {"snapshot_path", "history_path", "outbox_path", "lock_path",
                 "heartbeat_path", "block_path", "rate_path", "session_path"},
}

# 三个轮询间隔的下限。低于 MIN 拒绝启动，低于 WARN 只记一条警告。
#
# 这是给"觉得 30 分钟太慢"的人准备的护栏。旧校验是 minimum=1，填 60 能过，
# 然后每分钟打一次教务系统。轮询确实不等于登录（会话能复用，登录闸另算），
# 但每天上千次 GET 对一个学校系统仍然是很大的量。
#
# 300 秒是保守下限。修改代码常量的门槛高于误填配置值，
# 可以降低因配置错误产生高频请求的风险。
MIN_INTERVAL_SECONDS = 300
WARN_INTERVAL_SECONDS = 900
_INTERVAL_KEYS = ("interval_seconds", "active_interval_seconds",
                  "idle_interval_seconds")

# 三档从快到慢。默认值直接引 ScheduleConfig 的字段默认，不在这儿抄第二遍——
# 抄了就有两处要同时改，而这种地方的不一致过去已经发生过两次。
_INTERVAL_ORDER = (
    ("active_interval_seconds", "加速档", ScheduleConfig.active_interval),
    ("interval_seconds", "常规档", ScheduleConfig.interval),
    ("idle_interval_seconds", "省电档", ScheduleConfig.idle_interval),
)

_STORAGE_DEFAULTS = {
    "snapshot_path": "data/grades.json",
    "history_path": "data/history.jsonl",
    "outbox_path": "data/pending.json",
    "lock_path": "data/jwgrade.lock",
    "heartbeat_path": "data/last_push.marker",
    "block_path": "data/login_blocked",
    "rate_path": "data/login_rate.json",
    "session_path": "data/session.json",
}


def load(path: str | Path = "config.yaml", require_push: bool = True) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到配置文件 {p}，请先复制 config.example.yaml 为 config.yaml")

    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} 不是合法的 YAML：{e}") from e
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ConfigError(f"{p} 的顶层必须是键值对，当前是 {type(cfg).__name__}")

    _check_sections(cfg)

    # 密码不在这里解析。它交给 credentials.resolve()，这样 --set-password
    # 在还没有任何密码的情况下也能正常跑起来。
    cfg.setdefault("account", {})
    _anchor_storage_paths(cfg, p)

    notify = cfg.setdefault("notify", {})
    pp = notify.setdefault("pushplus", {})
    if not isinstance(pp, dict):
        raise ConfigError(f"notify.pushplus 必须是键值对，当前是 {type(pp).__name__}")
    if pp.get("enabled") and not pp.get("token"):
        pp["token"] = os.environ.get("PUSHPLUS_TOKEN", "")

    _validate(cfg, require_push)
    return cfg


def _anchor_storage_paths(cfg: dict, config_path: Path) -> None:
    """把运行状态路径固定到配置文件所在项目，避免不同启动目录各写一份。"""
    storage = cfg.setdefault("storage", {})
    if not isinstance(storage, dict):
        return
    base = config_path.expanduser().resolve().parent

    def resolve(raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        return path.resolve()

    values = {key: storage.get(key, default)
              for key, default in _STORAGE_DEFAULTS.items()}
    raw_rate = storage.get("rate_path")
    raw_session = storage.get("session_path")
    if ("rate_path" in storage and "session_path" not in storage
            and isinstance(raw_rate, str) and raw_rate.strip()):
        values["session_path"] = str(resolve(raw_rate).parent / "session.json")
    elif ("session_path" in storage and "rate_path" not in storage
          and isinstance(raw_session, str) and raw_session.strip()):
        values["rate_path"] = str(resolve(raw_session).parent / "login_rate.json")

    for key, raw in values.items():
        if not isinstance(raw, str) or not raw.strip():
            continue                 # 交给 _validate() 报清晰的配置错误
        storage[key] = str(resolve(raw))


def validate_runtime_storage(cfg: dict) -> tuple[Path, Path]:
    """认证前确认状态文件都能读写。

    会话和限速这两个必须同目录（它们是一对，分开放会出现"检查的是 A、
    程序用的是 B"）；其余状态文件只查权限，不限制位置。

    为什么要查其余的：loginrate 和 session_store 写文件时会保住原属主，
    另外四个不会。root 手工跑一次程序就可能把文件抢走，而后果各不相同——
    最隐蔽的是 grades.json，读不了会被封存并静默重建基线，快照没了你还
    不知道；login_blocked 读不了则会连 --unlock-login 一起卡住。
    与其在四个模块里重复属主保护，不如在认证之前就把问题暴露出来。
    """
    storage = cfg.get("storage") or {}
    rate = Path(storage.get("rate_path") or _STORAGE_DEFAULTS["rate_path"])
    session = Path(storage.get("session_path") or _STORAGE_DEFAULTS["session_path"])
    rate = rate.expanduser().resolve()
    session = session.expanduser().resolve()

    if rate.parent != session.parent:
        raise ConfigError(
            "storage.rate_path 和 storage.session_path 必须位于同一个状态目录；"
            f"当前分别是 {rate.parent} 和 {session.parent}")

    state_dir = rate.parent
    if not state_dir.is_dir():
        raise ConfigError(f"登录状态目录不存在：{state_dir}")
    if not os.access(state_dir, os.R_OK | os.W_OK | os.X_OK):
        raise ConfigError(f"登录状态目录不可读写：{state_dir}")

    # 只查已经存在的：首次运行时 history.jsonl / pending.json 本来就不该在。
    for key in _STORAGE_DEFAULTS:
        path = Path(storage.get(key) or _STORAGE_DEFAULTS[key])
        path = path.expanduser().resolve()
        if not path.exists():
            continue
        if not path.is_file():
            raise ConfigError(f"状态路径不是普通文件：{path}")
        if not os.access(path, os.R_OK | os.W_OK):
            raise ConfigError(f"状态文件不可读写（属主或权限不对）：{path}")
    return rate, session


def _check_sections(cfg: dict) -> None:
    unknown = sorted(set(cfg) - set(_SECTIONS) - _DEPRECATED_SECTIONS, key=str)
    if unknown:
        raise ConfigError("未知配置段：" + ", ".join(map(str, unknown)) +
                          "。如果是旧版 web 配置，请删除或迁移它")

    bad = [k for k in _SECTIONS
           if k in cfg and cfg[k] is not None and not isinstance(cfg[k], dict)]
    if bad:
        nl = chr(10) + "  - "
        raise ConfigError("这几段必须是键值对：" + nl + nl.join(
            f"{k} 当前是 {type(cfg[k]).__name__}" for k in bad))
    # None 段（写了段名没写内容）统一成空字典，后面就不用到处判空
    for k in _SECTIONS:
        if cfg.get(k) is None and k in cfg:
            cfg[k] = {}

    for section in _SECTIONS:
        value = cfg.get(section)
        if isinstance(value, dict):
            _check_keys(value, section)

    notify = cfg.get("notify") or {}
    pushplus = notify.get("pushplus") if isinstance(notify, dict) else None
    if isinstance(pushplus, dict):
        _check_keys(pushplus, "notify.pushplus")

    if "web" in cfg:
        log.warning("配置段 web 已移除，当前版本不会启动网页服务；请从配置中删除")


def _check_keys(value: dict, path: str) -> None:
    unknown = sorted(set(value) - _KEYS[path], key=str)
    if unknown:
        names = ", ".join(f"{path}.{key}" for key in unknown)
        raise ConfigError(f"存在未知配置项：{names}")


def rounds_per_day(interval_seconds, quiet_hours=()) -> int:
    """按这个间隔一天大约查多少轮。

    "1800 秒"没人判断得了，"每天 36 轮"可以——所有报错和 --preflight 都用
    这个数说话。对不合法的 quiet_hours 要能容错：校验它的代码在后面，
    这里先被调用。
    """
    active_hours = 24
    try:
        if quiet_hours and len(quiet_hours) == 2:
            start, end = (int(h) for h in quiet_hours)
            active_hours = 24 - (end - start) % 24
    except (TypeError, ValueError):
        active_hours = 24
    return max(0, round(active_hours * 3600 / max(1, int(interval_seconds))))


def interval_problem(label: str, value: int, quiet_hours=()) -> str | None:
    """间隔太密返回一句可操作的说明；偏快只记一条警告并返回 None。

    **配置里的三个间隔和命令行 --interval 共用这一条。** 2026-08-20 发现
    --interval 完全绕过了配置这道校验——它在校验之后直接改 ScheduleConfig
    的字段，只被 next_delay() 的 max(60, ...) 兜着，等于每分钟打一次学校。
    共用同一个函数，两条路的下限才不会各说各话。
    """
    if value >= WARN_INTERVAL_SECONDS:
        return None
    if value < MIN_INTERVAL_SECONDS:
        return (f"{label} 是 {value} 秒（约每天 "
                f"{rounds_per_day(value, quiet_hours)} 轮），低于下限 "
                f"{MIN_INTERVAL_SECONDS} 秒。过于频繁的查询可能增加学校系统负担或触发"
                f"安全策略；建议不要低于 {WARN_INTERVAL_SECONDS} 秒（约每天 "
                f"{rounds_per_day(WARN_INTERVAL_SECONDS, quiet_hours)} 轮）")
    log.warning("%s 是 %d 秒，约每天 %d 轮，明显快于默认设置。"
                "如果这是有意设置，可以忽略本警告。",
                label, value, rounds_per_day(value, quiet_hours))
    return None


def _int_problem(path: str, value, minimum: int) -> str | None:
    """整数校验。

    用 type(v) is int 而不是 isinstance：bool 是 int 的子类，
    isinstance(True, int) 为真且 True > 0 也成立，于是 interval_seconds: true
    会被当成 1 秒接受，从而产生每秒一次的异常请求。
    """
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        return f"{path} 必须是不小于 {minimum} 的整数，当前是 {value!r}"
    return None


def _bool_problem(path: str, value) -> str | None:
    """布尔校验。

    YAML 里 `enabled: "false"` 是字符串，而非空字符串为真——照这么写，
    用户可能以为已经关闭推送，但程序仍会继续发送。这类配置含义与实际行为相反的问题
    只能靠类型检查挡。
    """
    if value is None:
        return None
    if type(value) is not bool:
        return f"{path} 必须是 true 或 false（不加引号），当前是 {value!r}"
    return None


def _validate(cfg: dict, require_push: bool = True) -> None:
    """校验配置里那些填错了会静默出怪事的值。

    应在启动阶段直接报错退出，避免运行一段时间后才暴露配置问题。
    """
    problems: list[str] = []

    def check_int(path: str, value, minimum: int = 1) -> None:
        msg = _int_problem(path, value, minimum)
        if msg:
            problems.append(msg)

    def check_bool(path: str, value) -> None:
        msg = _bool_problem(path, value)
        if msg:
            problems.append(msg)

    account = cfg.get("account") or {}
    adapter = account.get("adapter", "mock")
    adapter_names = available_names()
    if not isinstance(adapter, str) or adapter not in adapter_names:
        problems.append(
            f"account.adapter 只能是 {', '.join(adapter_names)}，当前是 {adapter!r}")
    username = account.get("username")
    if not isinstance(username, str) or not username.strip():
        problems.append(
            f"account.username 必须是非空字符串（学号要加引号），当前是 {username!r}")

    sched = cfg.get("schedule") or {}
    check_bool("schedule.adaptive", sched.get("adaptive"))
    quiet = sched.get("quiet_hours")
    for key in ("active_duration_minutes", "idle_after_hours", "fail_alert_after"):
        check_int(f"schedule.{key}", sched.get(key))
    check_int("schedule.jitter_seconds", sched.get("jitter_seconds"), minimum=0)

    # 三个间隔决定请求学校系统的频率，因此使用更严格的下限。
    interval_type_error = False
    for key in _INTERVAL_KEYS:
        value = sched.get(key)
        problem = _int_problem(f"schedule.{key}", value, 1)
        if problem:
            problems.append(problem)
            interval_type_error = True
            continue
        if value is None:
            continue
        too_dense = interval_problem(f"schedule.{key}", value, quiet)
        if too_dense:
            problems.append(too_dense)

    # 顺序：加速 ≤ 常规 ≤ 省电。反了既不更费流量也不报错，但程序做的事会和
    # 字面意思相反——出分之后反而变慢。这类「能跑、行为却是反的」配置最难
    # 自己发现：日志一切正常，只是通知来得比平时晚，而你根本不会去怀疑配置。
    #
    # 相等是允许的（那只是把某一档关掉），只拦真正颠倒的。
    #
    # 只在 adaptive 开着时查：关掉时另外两档根本不参与计算，为用不上的字段
    # 此时无需阻止启动；重新启用 adaptive 时会再次执行校验。
    #
    # 缺的键按默认值补齐再比——只改了 interval_seconds 的配置同样可能是反的
    # （加速 900 / 常规 300），而那恰恰是最自然的改法。
    if not interval_type_error and sched.get("adaptive", True) is not False:
        resolved = []
        for key, label, default in _INTERVAL_ORDER:
            value = sched.get(key)
            resolved.append((key, label,
                             value if type(value) is int else default))
        for (fast_key, fast_label, fast), (_, slow_label, slow) in pairwise(
                resolved):
            if fast > slow:
                problems.append(
                    f"schedule.{fast_key} 是 {fast} 秒（{fast_label}），比"
                    f"{slow_label}的 {slow} 秒还慢。三档的意思是「越可能出分"
                    "越查得勤」，顺序必须是 加速 ≤ 常规 ≤ 省电——照现在这样，"
                    "检测到成绩变化后反而会降低检查频率")

    if quiet is not None and quiet != []:
        ok = (isinstance(quiet, (list, tuple)) and len(quiet) == 2
              and all(type(h) is int and 0 <= h <= 23 for h in quiet))
        if not ok:
            problems.append(
                f"schedule.quiet_hours 应形如 [1, 7]，两个 0-23 的整数，当前是 {quiet!r}")

    notify = cfg.get("notify") or {}
    # 0 表示关掉报平安
    check_int("notify.heartbeat_days", notify.get("heartbeat_days"), minimum=0)
    level = notify.get("detail_level")
    if level is not None and level not in ("full", "brief"):
        problems.append(f"notify.detail_level 只能是 full 或 brief，当前是 {level!r}")

    # 「开了推送但没给 token」是最危险的一种配置错误：build() 会返回空通道列表，
    # 而空列表在推送环节被当成"没什么可发的，算成功"，于是快照照常推进——
    # 成绩变化被永久跳过，你还以为推送开着。必须在启动时就拦死。
    #
    # 但只在这轮真要推送时才强制：--history / --set-password 是只读操作，
    # 而服务器上 token 放在 /etc/jwgrade.env 里，手动敲这些命令时环境变量
    # 并不在，没理由因此把人拦在门外。
    pp = notify.get("pushplus") or {}
    check_bool("notify.pushplus.enabled", pp.get("enabled"))
    if require_push and pp.get("enabled") and not pp.get("token"):
        problems.append(
            "notify.pushplus.enabled 是 true 但没有 token。"
            "填进 config.yaml 的 notify.pushplus.token，"
            "或设环境变量 PUSHPLUS_TOKEN；确实不想推送就把 enabled 改成 false")

    safety = cfg.get("safety") or {}
    check_int("safety.max_withdrawals", safety.get("max_withdrawals"))
    # 一小时最多登录几次。2026-08-16 账号因「频繁登录」被冻结过一次，
    # 该限制用于避免认证请求过于频繁，不应设置得过于宽松。
    check_int("safety.max_logins_per_hour", safety.get("max_logins_per_hour"))
    check_int("safety.max_password_logins_per_day",
              safety.get("max_password_logins_per_day"))
    check_int("safety.withdraw_confirm_rounds",
              safety.get("withdraw_confirm_rounds"))

    storage = cfg.get("storage") or {}
    normalized_paths: dict[str, str] = {}
    for key in ("snapshot_path", "history_path", "outbox_path", "lock_path",
                "heartbeat_path", "block_path", "rate_path", "session_path"):
        value = storage.get(key)
        if key in storage and (not isinstance(value, str) or not value.strip()):
            problems.append(
                f"storage.{key} 必须是非空路径字符串，当前是 {value!r}")
        elif isinstance(value, str) and value.strip():
            normalized = os.path.normcase(os.path.abspath(os.path.expanduser(value)))
            previous = normalized_paths.get(normalized)
            if previous:
                problems.append(
                    f"storage.{key} 与 storage.{previous} 不能相同：{value!r}")
            else:
                normalized_paths[normalized] = key

    if problems:
        nl = chr(10) + "  - "
        raise ConfigError("配置有问题：" + nl + nl.join(problems))
