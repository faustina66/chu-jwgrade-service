"""长安大学本科教务系统适配器。

登录链路：ids.chd.edu.cn 统一身份认证（金智 authserver / CAS）
          -> 带 ticket 回跳 -> bkjw.chd.edu.cn/eams 建立 session。

密码加密算法与登录页的 encrypt.js 保持一致：
    base64( AES-CBC(key=pwdEncryptSalt, iv=随机16字符, 随机64字符 + 明文密码) )
前面那 64 个随机字符是盐，服务端会丢弃；没有它同一个密码每次密文都相同。
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# `Crypto` 命名空间由仍在维护的 PyCryptodome 提供，不是 Bandit B413 所指的
# 已废弃 PyCrypto；requirements 也明确锁定为 pycryptodome。
from Crypto.Cipher import AES  # nosec B413
from Crypto.Util.Padding import pad  # nosec B413

from ..models import Grade
from ..redact import redact_url
from .base import (
    Adapter,
    LoginFailed,
    LoginNeedsHuman,
    LoginTransient,
    SessionExpired,
)

log = logging.getLogger(__name__)

IDS = "https://ids.chd.edu.cn"
EAMS = "http://bkjw.chd.edu.cn/eams"

# encrypt.js 里的 $aes_chars，刻意剔除了易混淆的 I/O/l/o/q/u/v/g/0/1/9
_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

# Tomcat 在会话第一次建立、还不确定客户端收不收 cookie 时，会把 session id
# 塞进 URL（;jsessionid=xxx）。这种写法是会话固定攻击的经典载体，教务系统
# 前面的安全设备直接返回 403。我们已经从 Set-Cookie 拿到同一个 id 了，
# URL 里那份纯属多余，跟跳转时剥掉即可。
_JSESSIONID_IN_URL = re.compile(r";jsessionid=[^?/#]*", re.IGNORECASE)


_REDIRECT_CODES = (301, 302, 303, 307, 308)


def strip_jsessionid(url: str) -> str:
    return _JSESSIONID_IN_URL.sub("", url)

# 成绩页候选路径，逐个试直到解析出表格。
#
# historyCourseGrade 排第一，因为它一次返回全部学期且不需要参数。search.action
# 是按学期分页的，缺 semesterId 会直接 500（Struts2 拿不到必填参数就抛异常），
# 而学期 ID 是会随学年作废的魔法数字，能不依赖就不依赖。
#
# 获取全部学期还可以覆盖记录在旧学期下的补考和重修成绩。
_GRADE_PATHS = [
    "/teach/grade/course/person!historyCourseGrade.action?projectType=MAJOR",
    "/teach/grade/course/person!historyCourseGrade.action",
]

# 表头中文名 -> 字段。同一字段多个候选，靠前的优先级高。
# score 的顺序是有讲究的：补考重修后「最终」和「总评」会不一样，以「最终」为准。
_HEADER_MAP = {
    "term": ["学年学期", "学期", "开课学期"],
    "course_id": ["课程代码", "课程序号", "课程编号", "选课课号"],
    "course_name": ["课程名称", "课程"],
    "credit": ["学分"],
    "score": ["最终", "最终成绩", "总评成绩", "总评", "期末成绩", "成绩"],
    "gpa": ["绩点", "平均绩点", "课程绩点"],
}

# 这两个字段一旦落到备选列上，**含义就变了**，必须喊一声。
# 见 _warn_fallback_columns —— 静默回退不会报错，它撒谎。
_PREFERRED = {
    # 「最终」是补考/重修**之后**的分；「总评」「期末」是补考之前的。
    # 悄悄换列 = 推给你一个看着完全正常、实际过时的数字。
    "score": ("最终", "最终成绩"),
    # 它是 Grade.key 的一半。换一列，所有课的键都变，一整轮全被判成新增。
    "course_id": ("课程代码",),
}

# 下列名称可能是其他列名的子串，因此只允许完全匹配。
# 「成绩」是「期中成绩」的子串——用它子串匹配会把期中分当成最终成绩；
# 「课程」是「课程代码」的子串，同理会把课程代码当成课程名。
_AMBIGUOUS = {"成绩", "课程", "总评", "最终"}


def _random_string(n: int) -> str:
    return "".join(secrets.choice(_AES_CHARS) for _ in range(n))


def encrypt_password(password: str, salt: str) -> str:
    """复刻 encrypt.js 的 encryptAES()。salt 为空时服务端接受明文。"""
    if not salt:
        return password
    key = salt.strip().encode("utf-8")
    iv = _random_string(16).encode("utf-8")
    data = (_random_string(64) + password).encode("utf-8")
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(data, AES.block_size))
    return base64.b64encode(ciphertext).decode("ascii")


class ChdAdapter(Adapter):
    name = "chd"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.service = cfg.get("service_url") or f"{EAMS}/home.action"
        self.debug_dump = bool(cfg.get("debug_dump"))
        # dump 的保存位置由上层指定，不能按当前工作目录解析：这份文件里有姓名、学号
        # 和全部成绩，落在哪必须是确定的。上层传的是状态目录，和快照同一处。
        self.dump_dir = Path(cfg.get("state_dir") or "data")
        self.semester_id = str(cfg.get("semester_id") or "").strip()

    @property
    def grade_paths(self) -> list[str]:
        paths = list(_GRADE_PATHS)
        if self.semester_id:
            # 配了学期就优先查它。注意 _fetch 是「第一条取到数据就返回」，
            # 所以这等于只盯这一个学期，新学期不会被发现——仅供排查用，
            # 常规监控必须留空。
            paths.insert(0, "/teach/grade/course/person!search.action"
                            f"?semesterId={self.semester_id}&projectType=")
        return paths

    # ------------------------------------------------------------------ 登录

    def _resume_via_sso(self, login_url: str, params: dict) -> bool:
        """不提交密码，只拿一张新 ticket。

        登录时提交了 rememberMe（页面上的「7天免登录」），CAS 会留一个长期
        TGT。只要它还有效，带 service 参数访问登录页就会被直接 302 送回教务
        系统并附带新 ticket，整个过程无需提交密码。

        这条路径是必要功能：EAMS 的 JSESSIONID 大约一个多小时后失效，旧版本
        会在每次失效后重新提交密码。2026-08-16，账号曾因此被统一身份认证判为
        「频繁登录」并冻结 10 分钟。

        如果缺少这条路径，7 天免登录生效后反而会导致会话恢复失败：
        原来那个带自动跳转的 GET 会落在教务系统首页上，页面里找不到
        pwdEncryptSalt，于是每一轮都以"页面结构可能已改版"失败。
        """
        self._notify_ticket_start()
        try:
            r = self.session.get(login_url, params=params, timeout=20,
                                 allow_redirects=False)
        except requests.RequestException as e:
            safe = redact_url(str(e)) or type(e).__name__
            raise LoginTransient(f"访问认证服务器失败：{safe}") from None

        if r.status_code not in _REDIRECT_CODES:
            return False          # 返回登录表单说明 TGT 已失效，需要执行完整登录流程

        self._follow(r.headers.get("Location"))
        if self._has_eams_session():
            self._notify_ticket_success()
            log.info("复用统一身份认证会话（7天免登录），未提交密码")
            return True
        # 跳转跟完了却没拿到教务系统会话，当作没复用成功，退回完整登录
        return False

    def login(self) -> None:
        login_url = f"{IDS}/authserver/login"
        params = {"service": self.service}
        if self._resume_via_sso(login_url, params):
            return

        # 换票没成功，说明这一轮必然要提交密码。查额度要早、记账要晚，
        # 这是两件事：
        #   查额度必须赶在下面两个请求之前——额度用完时「取登录页」和
        #   「带学号的验证码探测」本就不该发出去，后者是风控最敏感的信号。
        #   记账必须等到真的提交了才做——登录页拿不到、验证码探测失败时
        #   密码根本没交出去，凭什么消耗掉一天一次的额度、还留个阻断标记。
        self._notify_password_gate()

        r = self.session.get(login_url, params=params, timeout=20)
        r.raise_for_status()
        if "请连接校园网" in r.text:
            raise LoginTransient("被校园网 IP 白名单拦截，当前出口 IP 访问不了认证服务器")

        soup = BeautifulSoup(r.text, "lxml")
        salt = self._hidden(soup, "pwdEncryptSalt")
        lt = self._hidden(soup, "lt")
        execution = self._hidden(soup, "execution") or "e1s1"
        if salt is None:
            raise LoginTransient("登录页没找到 pwdEncryptSalt，页面结构可能已改版")

        self._assert_no_captcha()

        payload = {
            "username": self.username,
            "password": encrypt_password(self.password, salt),
            "captcha": "",
            "_eventId": "submit",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "lt": lt or "",
            "execution": execution,
            # 对应页面上的「7天免登录」，能拉长会话、减少登录次数
            "rememberMe": "true",
        }
        # 到这儿才是真的要交密码：记账 + 落阻断标记
        self._notify_password_submit()
        resp = self.session.post(
            login_url,
            params={"service": self.service},
            data=payload,
            headers={"Referer": r.url},
            timeout=20,
            allow_redirects=False,      # 跳转要自己跟，见 _follow()
        )
        # 认证失败时 CAS 回什么状态码，各校配置不同。**长安大学实测
        # 实际测试中，密码错误返回 401，而不是 200。
        #
        # 老代码只在 200 分支里做分类，401 直接落到 raise_for_status()，
        # 抛出原始 HTTPError —— 于是退出码是 1 而不是 20，后果一串：
        #   · setup.sh 的三次重试只认 20，完全不触发
        #   · 微信告警发不出去（未捕获异常在推送之前就逃走了）
        #   · 阻断标记停在 armed（「结果没能确认」），而实际结果是明确的
        #   · systemd 的 RestartPreventExitStatus 不包含 1，会额外重启一次
        if resp.status_code in (200, 401, 403):
            # 正文里认得出的提示优先——只有它能区分「密码错」和「验证码 / 锁定」
            self._raise_for_login_error(resp.text)
            if resp.status_code == 200:
                raise LoginTransient(
                    "登录未通过，认证服务器重新渲染了登录页，页面可能已改版")
            # 4xx 但正文里读不出理由：认证服务器明确拒绝了，可我们不知道为什么。
            # 归到 needs_human 而不是 LoginFailed —— **没有证据证明密码是错的**，
            # 不该逼人先改密码才能恢复。两者都退 20、都立刻停机，
            # 两者的区别在于解锁方式：needs_human 允许使用原密码解锁。
            raise LoginNeedsHuman(
                f"认证被拒绝（HTTP {resp.status_code}），"
                "但页面上没有认得出的错误提示。"
                "最常见的原因是密码错误，也可能是验证码或账号被临时锁定。"
                "请去 https://ids.chd.edu.cn 手动登录一次确认。")
        resp.raise_for_status()

        final = self._follow(resp.headers.get("Location"))
        # 回跳地址可能带着 ?ticket=ST-xxx。它会进日志，失败时还会随告警推给
        # PushPlus——一次性也好、短寿也好，凭据就不该往外走。
        shown = redact_url(final)
        if final is None or "/authserver/login" in final:
            raise LoginFailed(f"登录后仍停留在认证页，未拿到 ticket: {shown}")
        if not self._has_eams_session():
            raise LoginTransient(f"登录后没拿到教务系统会话 cookie，落地于 {shown}")
        log.info("登录成功，落地于 %s", shown)

    def resume_from_cookies(self) -> bool:
        if not self._has_eams_session():
            return False
        self._logged_in = True
        return True

    def _has_eams_session(self) -> bool:
        """登录成功与否，看的是有没有拿到 EAMS 的会话 cookie。"""
        return any(c.name == "JSESSIONID" and "bkjw" in (c.domain or "")
                   for c in self.session.cookies)

    def _follow(self, location: str | None, max_hops: int = 8) -> str | None:
        """手动跟随跳转链，逐跳剥掉 URL 里的 ;jsessionid=。

        两点和 requests 自带的 allow_redirects 不同：
        1. 剥掉 URL 里的 session id（Tomcat 首次会话会塞进去，是冗余的）
        2. 不对落地页的状态码报错 —— 登录要的是会话，不是页面。这套系统里
           index.action 落地会 403、成绩页缺参数会 500，但两种情况下 session
           都已经建好了，取成绩完全不受影响。
        """
        url = location
        for _ in range(max_hops):
            if not url:
                return None
            url = strip_jsessionid(url)
            try:
                r = self.session.get(url, timeout=20, allow_redirects=False)
            except requests.RequestException as e:
                safe = redact_url(str(e)) or type(e).__name__
                raise RuntimeError(f"登录跳转请求失败：{safe}") from None
            if r.status_code not in _REDIRECT_CODES:
                if not r.ok:
                    shown = redact_url(url) or ""
                    log.info("落地页 %s 返回 %d，忽略（只要会话建立即可）",
                             shown.rsplit("/", 1)[-1], r.status_code)
                return url
            location = r.headers.get("Location")
            url = urljoin(url, location) if location else None
        raise LoginTransient(f"跳转超过 {max_hops} 次仍未结束，可能陷入重定向循环")

    @staticmethod
    def _hidden(soup: BeautifulSoup, element_id: str) -> str | None:
        tag = soup.find("input", id=element_id)
        return tag.get("value", "") if tag else None

    def _assert_no_captcha(self) -> None:
        """对应登录页的 checkUserCaptcha()。密码连错几次后会被翻成 true。"""
        try:
            r = self.session.get(
                f"{IDS}/authserver/checkNeedCaptcha.htl",
                params={"username": self.username, "_": int(time.time() * 1000)},
                timeout=10,
            )
            data = r.json()
        except LoginFailed:
            raise
        except (requests.RequestException, ValueError) as e:
            # 网络抖动或返回的不是 JSON。探测失败不该阻断登录，继续走正常流程。
            # 不用宽泛的 except Exception：那会连自己写错的 AttributeError
            # 一起吞掉，于是探测永远"静默失败"，谁也不知道它其实早就不工作了。
            log.debug("验证码探测异常，忽略: %s", e)
            return
        # 合法 JSON 也可能不是对象——接口异常时返回过 null 和数组，那时
        # 否则 .get() 会抛出 AttributeError，导致本轮任务中断。
        # 显式判类型而不是加宽上面那个 except：加宽会连自己写错的
        # AttributeError 一起吞掉，那正是上面那段注释拒绝的事。
        if not isinstance(data, dict):
            log.debug("验证码探测返回的不是对象，忽略: %s", str(data)[:80])
            return
        need = data.get("isNeed")
        if need:
            # LoginNeedsHuman 而不是 LoginFailed：密码可能完全是对的，
            # 人在网页版过了验证码之后，拿**原密码**就该能恢复。
            raise LoginNeedsHuman(
                "认证服务器要求图形验证码（通常是密码连续错误触发）。"
                "请手动登录一次网页版解除，再执行 --unlock-login 恢复。"
            )

    @staticmethod
    def _raise_for_login_error(html: str) -> None:
        # 只有第一条是"密码被证伪了"，后两条不是——账号锁定和验证码都可能
        # 这也可能发生在密码正确的情况下，例如他人多次尝试该学号。单独抛出异常，
        # 恢复方式才能不同：前者必须换密码，后两者原密码就能解锁。
        for pattern, msg, needs_human in [
            ("用户名或密码错误", "用户名或密码错误", False),
            ("账号已被锁定|被锁定", "账号已被锁定", True),
            ("请输入验证码", "需要图形验证码", True),
        ]:
            if re.search(pattern, html):
                raise (LoginNeedsHuman if needs_human else LoginFailed)(msg)

    # ------------------------------------------------------------------ 取分

    def fetch_grades(self) -> list[Grade]:
        last_error: object = None
        for path in self.grade_paths:
            url = EAMS + path
            try:
                r = self.session.get(url, timeout=25, allow_redirects=True)
            except requests.RequestException as e:
                last_error = e
                continue

            # 被踢回认证页 = 会话过期，交给上层重新登录
            if "/authserver/login" in r.url or "统一身份认证" in r.text[:2000]:
                raise SessionExpired(f"访问 {path} 时被重定向回登录页")

            if not r.ok:
                # 403 常见于 URL 里混进了 ;jsessionid=，那是安全设备在拦
                last_error = f"{path} 返回 {r.status_code}"
                log.warning("%s", last_error)
                continue

            if self.debug_dump:
                name = path.split("!")[-1].split(".")[0].split("?")[0]
                fn = self.dump_dir / f"dump_{name}.html"
                # 这份 HTML 里有姓名、学号和全部成绩。按当前 umask 建出来可能是
                # 0644，同机器上任何账号都能读。开 0600，且要在写入前就定好权限，
                # 先建后 chmod 会留一个短暂的可读窗口。
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                with os.fdopen(os.open(fn, flags, 0o600), "w", encoding="utf-8") as f:
                    f.write(r.text)
                os.chmod(fn, 0o600)   # 文件已存在时 O_CREAT 的权限位不生效
                log.info("已保存原始页面到 %s（仅本人可读）", fn)

            grades = self._parse(r.text)
            if grades:
                log.info("从 %s 解析出 %d 条成绩", path, len(grades))
                return grades
            last_error = f"{path} 未解析出成绩表"

        raise RuntimeError(f"所有成绩页路径都没取到数据，最后一次: {last_error}")

    @staticmethod
    def _headers(table) -> list[str]:
        cells = [c.get_text(strip=True) for c in table.find_all("th")]
        if cells:
            return cells
        first = table.find("tr")
        return [c.get_text(strip=True) for c in first.find_all("td")] if first else []

    @staticmethod
    def _locate(headers: list[str]) -> dict[str, int]:
        """按表头名定位列号，而不是写死索引——教务系统改版最爱动列顺序。

        两轮匹配，顺序不能反：
        1. 完全相等。表头写得明确时，不该让子串匹配抢在前面。
        2. 必要时使用子串匹配，但跳过 _AMBIGUOUS 中可能与其他列名混淆的词。
        """
        norm = [h.strip() for h in headers]
        idx: dict[str, int] = {}
        for field, candidates in _HEADER_MAP.items():
            for cand in candidates:
                if cand in norm:
                    idx[field] = norm.index(cand)
                    break
            if field in idx:
                continue
            for cand in candidates:
                if cand in _AMBIGUOUS:
                    continue
                hit = next((i for i, h in enumerate(norm) if cand in h), None)
                if hit is not None:
                    idx[field] = hit
                    break
        return idx

    @staticmethod
    def _warn_fallback_columns(headers: list[str], idx: dict[str, int]) -> None:
        """用了备选列就喊一声。

        _HEADER_MAP 的候选链是为了扛住改版，但**回退是会改变含义的**。
        这类错误最坏的地方在于它不报错：通知照发，课程名、绩点、排版全对，
        只有那个数字是旧的，而你没有任何理由怀疑它。
        """
        for field, preferred in _PREFERRED.items():
            i = idx.get(field)
            if i is None or i >= len(headers):
                continue
            actual = headers[i].strip()
            if actual not in preferred:
                log.warning(
                    "解析用了备选列：%s 取自「%s」而不是「%s」。教务处可能改版了，"
                    "推出来的数字未必是你以为的那个。", field, actual, preferred[0])

    @classmethod
    def _parse(cls, html: str) -> list[Grade]:
        """解析成绩页。

        页面上有「主修成绩」「辅修成绩」两张表，列结构还不一样（辅修表没有成绩列）。
        所以遍历全部表格并合并，同时要求表里必须同时有课程名和成绩列——没有成绩列
        的表（比如空的辅修表）解析出来只会是一堆永远"未出分"的幽灵课程。
        """
        soup = BeautifulSoup(html, "lxml")
        collected: dict[str, Grade] = {}
        incomplete: list[str] = []      # 缺身份列被跳过的
        duplicated: list[str] = []      # 主键重复而被丢弃的

        # 老 Struts2 页面爱用表格套表格做布局。外层表的 find_all("th") 会把内层
        # 所有表头一起捞出来，拼成一份错位的列映射。
        #
        # **只处理叶子表，跳过外层表。** 旧逻辑仍会处理排在后面的外层表，
        # 那在多张内层表时侥幸没出事（错位表头过不了列定位那关），可页面上
        # 只有一张成绩表时，外层表捞到的表头恰好完整、行也是同一批——同一门
        # 同一课程可能被解析两次并触发重复检查，导致本轮任务异常退出。
        #
        # 外层表里的数据本来就是内层表的，处理它没有任何收益，只有风险。
        tables = [t for t in soup.find_all("table") if t.find("table") is None]

        for table in tables:
            headers = cls._headers(table)
            if not any("课程" in h for h in headers):
                continue
            idx = cls._locate(headers)
            if "course_name" not in idx or "score" not in idx:
                continue
            cls._warn_fallback_columns(headers, idx)

            # idx 用默认参数绑住：它是循环变量，闭包按引用捕获的话，
            # 将来谁把 pick 挪出循环或延后调用，取到的就是最后一张表的列映射。
            def pick(cells: list[str], field: str, idx: dict = idx) -> str:
                i = idx.get(field)
                return cells[i] if i is not None and i < len(cells) else ""

            body = table.find("tbody") or table
            for tr in body.find_all("tr"):
                cells = [
                    c.get_text(strip=True).replace("\xa0", "") for c in tr.find_all("td")
                ]
                if not cells or len(cells) < len(headers) - 2:
                    continue
                name = pick(cells, "course_name")
                if not name or name in headers:
                    continue
                term, course_id = pick(cells, "term"), pick(cells, "course_id")
                if not term or not course_id:
                    # 这两列拼起来就是 Grade.key —— 变化比对的身份依据。缺一个，
                    # 所有缺失标识的行可能生成同一个键，最终只保留第一条，使下一轮
                    # 误判为大量课程被撤回。与其合并错误数据，不如跳过无法识别的行。
                    incomplete.append(name)
                    continue
                g = Grade(
                    term=term,
                    course_id=course_id,
                    course_name=name,
                    score=pick(cells, "score"),
                    credit=pick(cells, "credit"),
                    gpa=pick(cells, "gpa"),
                    # strict=False 是有意的：教务处的表格常有单元格数少于
                    # 表头的行（末列为空时干脆不输出 <td>），严格模式会直接抛。
                    raw=dict(zip(headers, cells, strict=False)),
                )
                if g.key in collected:
                    duplicated.append(f"{name}({g.key})")
                    continue
                collected[g.key] = g

        # 页面结构异常时不采信本轮结果，避免使用不可靠数据更新快照。
        #
        # 旧逻辑只跳过异常行并记录警告，但缺少身份列或主键重复的记录会从快照中
        # 消失，并可能被误判为撤回。因此，这里必须
        # 拒绝整轮结果。否则用户可能收到看似正常的撤回通知，而真实原因只是页面结构变化。
        #
        # 解析结果为 0 条时已有保护（上方会抛出 RuntimeError），这里补充处理
        # 部分损坏的情况，例如少数行异常而其余记录仍可解析。
        #
        # 抛出异常后，本轮不推送也不更新快照，下一轮继续尝试；连续失败 5 轮后发送告警。
        # 页面改版时监控会暂停，但这比发送错误通知更安全。
        #
        # 「最终」列的回退逻辑保持不变：它只影响分数来源，异常时会记录 WARNING，
        # 并可在页面列名变化时维持基本解析能力。
        problems = []
        if incomplete:
            problems.append(
                f"{len(incomplete)} 行缺「学年学期」或「课程代码」"
                f"（{'、'.join(incomplete[:5])}）")
        if duplicated:
            problems.append(
                f"{len(duplicated)} 行的「学期::课程代码」和前面重复"
                f"（{'、'.join(duplicated[:5])}）")
        if problems:
            raise RuntimeError(
                "成绩页结构和预期不符，本轮不采信：" + "；".join(problems)
                + "。这两列共同构成变化比对的唯一标识，缺失或重复都可能让不同课程"
                "混成一门，进而产生错误的撤回通知。教务系统页面结构可能已经变化，"
                "可加 --dump 重新运行一次并保存原始页面。")
        return list(collected.values())
