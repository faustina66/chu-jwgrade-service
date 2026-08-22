"""微信推送。

目前只提供 PushPlus 通道。如需增加 Server酱、ntfy 或邮件等通道，可继承
Notifier 实现 send()，并在 build() 中注册相应配置。

正文使用 HTML，以改善微信消息详情页中的移动端排版效果。
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import ClassVar

import requests

from .models import Change

log = logging.getLogger(__name__)


class PushRejected(Exception):
    """接口明确拒收，本轮停止重试。

    此类错误不同于暂时性网络异常：token 无效时立即重试没有意义；接口已经
    判定请求过多时，继续发送还可能延长限制时间。

    发件箱会在下一轮补发（约半小时后），那才是合理的重试间隔。
    """

# 成绩页上「课程名称」之后的六项分数列，原样带进推送，缺的显示"空"
DETAIL_COLUMNS = ["期中成绩", "期末成绩", "平时成绩", "总评成绩", "实验成绩", "最终"]
# 明细每行放几项。六项拆成两行三列，读起来最整齐。
PER_ROW = 3


# 挂科判定。**严格小于**——60.0 算及格，60 分整不标红。
PASS_LINE = 60.0
# 绩点低于这个数也标红。挂科时绩点通常是 0，但两者分开判：
# 万一出现「分数及格、绩点异常」的行，也能看出来。
GPA_LINE = 1.0
# 非数字成绩（"优秀"/"通过"/"不及格"）没法比大小，只能认词。
# 认漏了的后果是该红的没红，认错了的后果是好成绩被标红——所以宁可列窄一点。
# 长安大学实际用哪个词还没见过真的挂科行，见到了往这里加。
FAIL_WORDS = ("不及格", "不合格", "未通过", "不通过", "缺考", "作弊", "取消成绩")


class Notifier:
    name = "notifier"

    def send(self, title: str, body: str) -> bool:
        raise NotImplementedError

    def _check(self, r: requests.Response) -> str | None:
        """检查业务层是否真的成功。返回错误说明，None 表示成功。"""
        return None

    def _post(self, url: str, *, retries: int = 3, **kwargs) -> bool:
        """推送失败时重试，尽量避免遗漏成绩通知。

        但只重试**可能是暂时的**那些。接口明确拒收时 _check 抛 PushRejected，
        这里立刻收手，见那个类的说明。
        """
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, timeout=15, **kwargs)
                r.raise_for_status()
                # HTTP 200 不等于推送成功：这类服务惯常把业务错误码塞在响应体里
                err = self._check(r)
                if err is None:
                    log.info("[%s] 推送成功", self.name)
                    return True
                log.warning("[%s] 接口返回失败 (%d/%d): %s", self.name, attempt, retries, err)
            except PushRejected as e:
                # 不显示 (n/3)，以免被误认为暂时性网络异常；此处需要用户检查配置。
                log.error("[%s] 接口拒收，已停止重试。%s", self.name, e)
                return False
            except requests.RequestException as e:
                log.warning("[%s] 请求失败 (%d/%d): %s", self.name, attempt, retries, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
        return False


class PushPlus(Notifier):
    name = "PushPlus"
    # 接口对正文长度有上限，官方文档没写数值，实测 25000+ 字符会被拒。
    # 保守取值；成绩单超长会在渲染层按学期拆条，这里只是最后一道保险。
    MAX_BODY = 9000

    # 下列错误不适合立即重试。提示内容会写入日志和告警，因此同时给出处理方法。
    # 官方返回码表：pushplus.plus/doc/guide/code.html
    FATAL: ClassVar[dict[str, str]] = {
        "900": "账号因请求过多而受到限制，请停止继续发送。等待限制解除后，"
               "发件箱会在下一轮重新尝试。",
        "903": "token 无效。请前往 pushplus.plus 重新复制 token，将其写入 "
               "/etc/jwgrade.env 的 PUSHPLUS_TOKEN，然后重启服务。此问题无法通过等待自动恢复。",
        "905": "账号还没实名认证，去 pushplus.plus 完成认证。",
        "401": "接口未授权，检查 pushplus 后台是否开启了开放接口。",
        "403": "请求 IP 未授权，检查 pushplus 后台的 IP 白名单。",
        "888": "积分不足，需要充值。",
    }

    def __init__(self, token: str):
        self.token = token

    def _check(self, r: requests.Response) -> str | None:
        try:
            data = r.json()
        except ValueError:
            return f"响应不是 JSON：{r.text[:200]}"
        if not isinstance(data, dict):
            # 接口异常时返回过 list 和 null。直接 .get() 会抛 AttributeError，
            # 否则异常会从 send() 直接抛出，调用方无法得到明确的失败结果，
            # 并可能导致本轮任务中断。
            return f"响应不是对象：{str(data)[:200]}"
        code = str(data.get("code"))
        if code in self.FATAL:
            # 903 曾被误写成「日额度」，与官方返回码表中的实际含义相反。
            # 这里按官方定义处理，并补充 900 的处理方法。
            raise PushRejected(f"code={code} —— {self.FATAL[code]}")
        if code != "200":
            #   999  「服务端验证错误」是通用错误码，至少对应三种情况：
            #        a) 推送过快。免费额度约每分钟 5 条，超出后可能连续失败；
            #        b) 短时间内重复发送完全相同的消息；
            #        c) 正文过长。
            #        排查时依次检查重复内容、发送频率和正文长度。
            return f"code={data.get('code')} msg={data.get('msg')!r}"
        return None

    def send(self, title: str, body: str) -> bool:
        """⚠️ 返回 True 只代表 **PushPlus 收到了请求**，不代表微信送到了。

        官方原话：「code为200仅代表服务端收到请求了，并不表示发送消息成功了」。
        响应里的 data 是消息流水号，可以拿去查最终状态（0未发送 / 1发送中 /
        2成功 / 3失败），也可以传 callbackUrl 等回调 —— 两个我们都没做。

        所以本程序的「推送成功」实际含义是**已提交给 PushPlus**。它接收之后
        再发失败，我们不知道，快照照常推进，那条变化不会重推。
        """
        if len(body) > self.MAX_BODY:
            log.warning("[%s] 正文 %d 字符超过上限 %d，截断",
                        self.name, len(body), self.MAX_BODY)
            body = _truncate_html(body, self.MAX_BODY)
        return self._post(
            "https://www.pushplus.plus/send",
            json={"token": self.token, "title": title,
                  "content": body, "template": "html"},
        )


# ---------------------------------------------------------------- 渲染零件

# 样式集中在一个 <style> 里，而不是每个元素挂 inline style。
# 27 门课用 inline 写法要 25000+ 字符，直接被 PushPlus 以「服务端验证错误」拒收；
# 换成类选择器后同样内容只要三分之一。后代选择器让内层 <b> <i> <span> 不用写 class。
_CSS = (
    # 全部用最保守的写法：小程序的 CSS 解析器比浏览器严格，
    # font 简写和小数 px 会被整条丢弃，字号行高一起掉回默认值（字更大、行更高）。
    # 属性一个个写、px 只用整数、不依赖标签默认样式。
    #
    # 实际设备测试表明，小程序只能可靠识别类选择器。带类型选择器的规则可能
    # 被忽略或降级到最外层的类上，例如 `.jw p b{color:蓝}` 可能把整段文字
    # 都染成蓝色，而规则本意只针对数值。因此颜色一律使用单独的类选择器，
    # 连 `.jw ` 前缀都不加。
    #
    # 移除 `.jw ` 前缀后，这些规则会作用于 PushPlus 整个页面，而不只当前 HTML。
    # .l、.e、.n、.c、.s 等短类名可能与宿主页面冲突。公众号和小程序测试
    # 均未发现冲突，因此暂时保留；如出现样式串扰，可改为 .jwl、.jwe 等
    # 带前缀的类名，但每门课程会增加十余字节。
    #
    # 实际设备测试表明，小程序会忽略 `b{}`，导致蓝色数字变成黑色，
    # 只有使用类选择器的「空」仍保持蓝色，因此数值需要统一使用 `.v`。
    #
    # 因此数值统一使用 `.v`。每门课程约增加 64 字节，单条消息容量从 10 门降至
    # 9 门；正常情况下每轮只有一两门课程，不受影响。`b{}` 规则保留用于兼容
    # 未设置 class 的 <b> 标签。
    "<style>"
    # word-break 规则用于解决手机上「最终100」被拆成
    # 「最终10」和下一行「0」的问题。PushPlus 页面为容器设置了
    # word-break:break-all（任意两字符之间都能断），而我们的类选择器全是裸的，
    # 宿主样式可以覆盖这些规则。iframe 隔离复现表明：break-all 下 260px 宽会拆分数字，
    # normal 不劈但会把「平时」拆成两半，keep-all 两样都不发生。
    #
    # .jw 使用 normal 覆盖宿主样式，明细行使用 .d 设置 keep-all。明细项之间有
    # " · "，仍可正常换行。标题保留 normal，避免较长且没有标点的课程名称
    # 整体超出屏幕。
    ".jw{font-family:-apple-system,sans-serif;font-size:15px;line-height:1.5;color:#000;word-break:normal}"
    ".d{word-break:keep-all}"
    # 两张表均采用「第一列 40%，其余列平分」的布局，使明细中的期末/实验与
    # 上方两列表中的分数、课程类别和绩点对齐。采用 4:6，是因为课程序号长度
    # 较固定，而课程类别可能较长。未设置 border-collapse，因为表格本身没有边框，
    # 且尚未确认小程序对该属性的支持情况。
    # ⚠️ 这三条的类名**故意不带引号**（`<table class=t>`），全文仅此一处。
    #
    # HTML5 允许属性值不加引号，浏览器仍可匹配并采用满宽、4:6 列宽布局。
    # 小程序解析器无法匹配这些无引号类名，因此会使用自身的默认表格样式；
    # 默认样式同样可以正确显示满宽自动列。
    #
    # 测试过带引号类名、内联 width 和 `.jw td` 后代选择器。前两种不能
    # 解决问题，后代选择器会被小程序识别并使第一列超出屏幕。
    # 最终保留无引号类名，用于隔离浏览器与小程序的表格样式。
    #
    # 因此，这几个表格类名不能添加引号。数值列的 class="v" 则必须带引号：
    # 前者需要避开小程序匹配，后者需要让小程序识别。
    ".t{width:100%;table-layout:fixed;word-break:keep-all}"
    ".t td{padding:1px 0;font-size:13px;color:#000}"
    ".t td:first-child{width:40%}"
    ".ta{width:100%;table-layout:fixed;word-break:keep-all}"
    ".ta td{padding:1px 0;font-size:13px;color:#000}"
    ".ta td:first-child{width:40%}"
    ".jw p{margin:3px 0 0;color:#000;font-size:13px;line-height:1.6;font-weight:400}"
    "b{color:#1156c4;font-weight:700;font-size:14px}"
    ".v{color:#1156c4;font-weight:700;font-size:14px}"
    ".l{color:#000;font-weight:400;font-size:13px}"
    ".e{color:#1156c4;font-weight:700;font-size:14px}"
    ".h{margin:20px 0 2px;font-size:17px;font-weight:700;color:#000}"
    ".hm{color:#555;font-size:13px;font-weight:400}"
    ".c{padding:10px 0;border-bottom:2px solid #dcdcdc;word-break:normal}"
    ".n{font-size:18px;font-weight:700;color:#000}"
    ".bar{color:#1156c4;font-weight:400}"
    ".s{color:#1156c4;font-weight:700;font-size:18px}"
    # 「旧 → 新」整块不拆：它很短（撑死七八十像素），断在箭头两边最难看。
    # 必要时将整组内容移到下一行，避免在「60 → 100」中间换行。
    ".w{color:#c5221f;font-weight:700;white-space:nowrap}"
    # 改完及格用的蓝色版「旧 → 新」。和 .w 只差颜色，字号故意不写死，
    # 两者必须一样大——否则同一个位置的字忽大忽小。
    ".wb{color:#1156c4;font-weight:700;white-space:nowrap}"
    ".ws{color:#000;font-weight:400}"
    ".g{margin:16px 0 0;padding:10px;background:#eef2f8;border-radius:6px;"
    "text-align:center;font-size:16px;color:#000}"
    ".gv{color:#1156c4;font-weight:700;font-size:20px}"
    # 变化类型所在行。样式设置在 div 上，而不是 p；原因见 _kind_line() 的说明。
    ".k{margin:0 0 1px;color:#1156c4;font-size:12px;font-weight:700}"
    ".kw{margin:0 0 1px;color:#c5221f;font-size:12px;font-weight:700}"
    # 课程序号。比正文小一号、不带蓝——它是标识不是分数；但颜色和下面的
    # 黑字一样深（#000），淡一档会显得像被划掉了。
    ".q{color:#000;font-size:12px;font-weight:400;white-space:nowrap}"
    # 挂科：明细里的数值和「空」用 .f，标题上那个大分数用 .fs。
    # 和撤回/变更共用同一个红（#c5221f）——都是"要注意"，没必要发明第二种红。
    ".f{color:#c5221f;font-weight:700;font-size:14px}"
    ".fs{color:#c5221f;font-weight:700;font-size:18px}"
    "</style>"
)


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def _truncate_html(body: str, limit: int) -> str:
    """在最后一个完整的 </div> 处切断，绝不从标签中间切。

    从中间截断会留下不完整标签，并被接口判为非法内容而拒收。
    """
    cut = body.rfind("</div>", 0, limit - 60)
    if cut < 0:
        return body[:limit]
    return body[:cut + 6] + '<p style="color:#999">（内容过长，已截断）</p></div>'


def term_label(term: str) -> str:
    """把「2025-2026 1」写成「2025-2026（1）」，让学期序号更醒目。

    认空格和连字符两种分隔，且只把**末尾 1~2 位**的数字当学期号——否则
    「2025-2026」会被切成「2025（2026）」。认不出来的原样返回：学期串是教务
    系统给的，格式换了也不该在这里报错，最多是不好看。
    """
    m = re.match(r"^(.*?)[ -]([0-9]{1,2})$", term.strip())
    return f"{m.group(1)}（{m.group(2)}）" if m else term


def _wrap(body: str) -> str:
    return f'{_CSS}<div class="jw">{body}</div>'


def _as_number(v):
    """能当数字用就返回 float，否则 None。教务系统的分数一律是字符串。"""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _is_failing(g) -> bool:
    """这门课算不算挂。

    只看**最终成绩**——`g.score` 取的就是「最终」那一列（见适配器的
    `_HEADER_MAP`，「最终」在候选里排第一）。补考重修之后作数的是它，
    总评再低也不看。还没出分、或者已被撤回（分数为空）都不算挂。
    """
    v = (g.score or "").strip()
    if not v:
        return False
    n = _as_number(v)
    if n is not None:
        return n < PASS_LINE
    return any(w in v for w in FAIL_WORDS)


def _low_gpa(g) -> bool:
    n = _as_number(g.gpa)
    return n is not None and n < GPA_LINE


def _detail(g, fail: bool = False) -> str:
    """六项分数明细。取自抓取时留存的原始表格行，缺的显示"空"。

    挂科时整行转红，包括显示为"空"的项目，避免不同颜色造成误解。
    """
    cells = []
    for col in DETAIL_COLUMNS:
        val = (g.raw.get(col) or "").strip()
        if fail:
            inner = f'<span class="f">{_esc(val) if val else "空"}</span>'
        else:
            inner = (f'<b class="v">{_esc(val)}</b>' if val
                     else '<span class="e">空</span>')
        label = col.replace("成绩", "")
        cells.append(f'<span class="l">{label}</span>&nbsp;{inner}')

    # 用表格而不是 " · " 拼接。值的宽度不一样（空 / 87 / 100 / 82.7），
    # 拼出来两行必然错位；<td> 天生按列对齐，**哪怕 CSS 被砍掉，列还是齐的**。
    #
    # 这是权衡后的选择：display:inline-block / flex / grid 在小程序里都会被
    # 砍掉，表格是唯一"降级之后仍然对齐"的手段。剩下的风险是小程序万一连
    # <table> 标签一起移除，六项内容可能会挤在一起。公众号端已验证正常。
    rows = [cells[i:i + PER_ROW] for i in range(0, len(cells), PER_ROW)]
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
        for row in rows if row)
    return f"<table>{body}</table>"


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _facts(g, fail: bool = False) -> str:
    """六项分数明细，三列表，两行。

    用表格而不是 " · " 拼接：值的宽度不一样（空 / 87 / 100 / 82.7），
    拼出来两行必然错位，而 <td> 天生按列对齐——**哪怕 CSS 被小程序砍掉，
    列还是齐的**。flex / grid / inline-block 都会被砍，表格是唯一
    "降级之后仍然对齐"的手段。
    """
    rows = []
    cells = []
    for col in DETAIL_COLUMNS:
        val = (g.raw.get(col) or "").strip()
        if fail:
            inner = f'<span class="f">{_esc(val) if val else "空"}</span>'
        else:
            inner = (f'<b class="v">{_esc(val)}</b>' if val
                     else '<span class="e">空</span>')
        label = col.replace("成绩", "")
        cells.append(f'<span class="l">{label}</span>&nbsp;{inner}')
    rows += [cells[k:k + PER_ROW] for k in range(0, len(cells), PER_ROW)]

    body = "".join(_row(r) for r in rows if r)
    return f'<table class=t>{body}</table>' if body else ""


def _ident(g, score_cell: str, with_term: bool = True,
           fail: bool = False) -> str:
    """序号 / 分数、学期 / 类别、学分 / 绩点，三行两列。

    第 2 列上下贯通：分数、课程类别、绩点都落在那儿，所以「100」的第一位、
    「学科…」的「学」、「绩点」的「绩」是一条线。
    第 1 列贴左边，和下面明细表的「期中」「总评」也是一条线。
    """
    rows = []
    seq = (g.raw.get("课程序号") or "").strip()
    # 课程代码不另外列：序号就是「代码 + 后缀」，12XK1101.07 已经含着它。
    if seq or score_cell:
        rows.append([f'<span class="q">[{_esc(seq)}]</span>' if seq else "",
                     score_cell])
    left = _esc(term_label(g.term)) if (with_term and g.term) else ""
    right = _esc(g.raw.get("课程类别") or "")
    if left or right:
        rows.append([f'<span class="l">{left}</span>' if left else "",
                     f'<span class="l">{right}</span>' if right else ""])
    if g.credit or g.gpa:
        credit = (f'<span class="l">学分</span><b class="v">{_esc(g.credit)}</b>'
                  if g.credit else "")
        gpa = ""
        if g.gpa:
            val = (f'<span class="f">{_esc(g.gpa)}</span>'
                   if fail or _low_gpa(g) else f'<b class="v">{_esc(g.gpa)}</b>')
            gpa = f'<span class="l">绩点</span>{val}'
        rows.append([credit, gpa])
    body = "".join(_row(r) for r in rows if any(r))
    return f'<table class=ta>{body}</table>' if body else ""


# ---------------------------------------------------------------- 变化通知

def _title(changes: list[Change]) -> str:
    withdrawals = [c for c in changes if c.is_withdrawal]
    # 撤回需要单独提示，不能使用普通出分通知的标题。
    if len(withdrawals) == len(changes):
        if len(changes) == 1:
            return f"⚠️ {changes[0].grade.course_name} 成绩被撤回"
        return f"⚠️ {len(changes)} 门成绩被撤回"
    if len(changes) == 1:
        c = changes[0]
        # 标题里不放分数。PushPlus 的消息详情页给标题也设了
        # word-break:break-all，「出分了：100」会被劈成「出分了：10」换行「0」，
        # 标题由 PushPlus 渲染，当前 HTML 中的 CSS 无法控制其样式。
        # 因此，锁屏通知只显示课程名称，分数需要打开消息后查看。
        if c.kind == "changed":
            return f"🔄 {c.grade.course_name} 成绩变更"
        return f"📢 {c.grade.course_name} 出分了"
    if withdrawals:
        return f"📢 教务系统更新了 {len(changes)} 门成绩（含 {len(withdrawals)} 门撤回）"
    return f"📢 教务系统更新了 {len(changes)} 门成绩"


def _kind_line(c: Change) -> str:
    """变化类型，单独一行放在课程名上面。

    用 `<div>` 不用 `<p>`：`.jw p{color:#000}` 的特异度（0,0,1,1）压得过
    `.k`（0,0,1,0），写成 `<p class="k">` 颜色会被打回黑色。`.h`/`.c` 也是
    出于同样的原因用 div。

    红色只用于撤回，以及修改后仍未及格的成绩。
    成绩修改本身不代表异常，例如补考后成绩提高时不应标红。
    """
    bad = c.kind == "withdrawn" or (c.kind == "changed" and _is_failing(c.grade))
    return f'<div class="{"kw" if bad else "k"}">{_esc(c.label)}</div>'


def _headline(c: Change, brief: bool = False) -> str:
    """课程名一行，[课程序号] 和分数另起一行。

    不同设备对长课程名称的换行位置不一致。将课程名称和分数拆行后，可以固定布局，
    并让分数与下一行的课程类别对齐（均位于第 2 列）。

    brief=True 时只保留课程名称和分数，不输出学期、课程序号、课程类别、学分
    和绩点，以减少发送给第三方推送服务的内容。这里必须检查 level，
    否则这些字段仍会发送，与配置说明不一致。
    """
    name = f'<span class="bar">▍</span><b class="n">{_esc(c.grade.course_name)}</b>'
    if c.kind == "withdrawn":
        score = (f'<span class="w"><s class="ws">{_esc(c.old_score)}</s>'
                 " → 已撤回</span>")
    elif c.kind == "changed":
        cls = "w" if _is_failing(c.grade) else "wb"
        score = (f'<span class="{cls}"><s class="ws">{_esc(c.old_score)}</s> → '
                 f'{_esc(c.grade.score)}</span>')
    else:
        cls = "fs" if _is_failing(c.grade) else "s"
        score = f'<span class="{cls}">{_esc(c.grade.score)}</span>'
    if brief:
        # 分数仍放在第 2 列：和 full 模式里它的位置一致，看着不会突兀。
        return name + f"<table class=ta>{_row(['', score])}</table>"
    return name + _ident(c.grade, score, fail=_is_failing(c.grade))


def render(changes: list[Change], level: str = "full") -> tuple[str, str]:
    """把变化列表渲染成推送标题和 HTML 正文。

    level 决定正文给第三方看多少：
        full     完整明细（推送服务能看到全部成绩）
        brief    只有课程名和分数
    """
    # 仍然按学期归堆（同学期的课排在一起），但**不再输出学期小标题**：
    # 每张卡的 meta 行开头已经写了学期，标题就是重复的。
    by_term: dict[str, list[Change]] = {}
    for c in changes:
        by_term.setdefault(c.grade.term or "未知学期", []).append(c)

    blocks = []
    for term in sorted(by_term):
        for c in by_term[term]:
            fail = _is_failing(c.grade)
            inner = _kind_line(c) + _headline(c, brief=level != "full")
            if level == "full":
                inner += _facts(c.grade, fail)
                if c.kind == "withdrawn":
                    inner += ('<p style="color:#c5221f">'
                              "教务处可能正在改分，请留意后续通知</p>")
            # brief 只给课程名和分数，学期都不给：它存在的意义就是少泄露一点
            # 给第三方推送服务，往里加字段是在拆它自己的目的。
            # _headline(brief=...) 负责真正省略这些字段，不能只在此处说明。
            blocks.append(f'<div class="c">{inner}</div>')
    return _title(changes), _wrap("".join(blocks))


# ---------------------------------------------------------------- 完整成绩单

def _weighted_gpa(grades) -> float | None:
    """学分加权平均绩点。绩点或学分不是数字的课（如"优秀"没绩点）自动跳过。"""
    credits = points = 0.0
    for g in grades:
        try:
            c, p = float(g.credit), float(g.gpa)
        except (TypeError, ValueError):
            continue
        credits += c
        points += c * p
    return points / credits if credits else None


def _term_block(term: str, rows: list) -> str:
    gpa = _weighted_gpa(rows)
    summary = f'<b class="v">{len(rows)}</b> 门' + (
        f' · 绩点 <b class="v">{gpa:.2f}</b>' if gpa is not None else "")
    out = [f'<div class="h">{_esc(term_label(term))}&nbsp;&nbsp;'
           f'<span class="hm">{summary}</span></div>']
    for g in sorted(rows, key=lambda x: x.course_name):
        fail = _is_failing(g)
        score = (f'<span class="{"fs" if fail else "s"}">{_esc(g.score)}</span>'
                 if g.has_score else '<span class="e">未出分</span>')
        # 学期已经写在分组标题上了，每门课再重复一遍纯属浪费长度。
        # 这一点和变化通知不同：那边一条消息里可能混着几个学期，这边一条就是
        # 一个学期，而且标题还带着「N 门 · 绩点 X」，不只是个学期名。
        out.append(f'<div class="c"><span class="bar">▍</span>'
                   f'<b class="n">{_esc(g.course_name)}</b>'
                   f"{_ident(g, score, with_term=False, fail=fail)}{_facts(g, fail)}</div>")
    return "".join(out)


def render_report(grades, budget: int = 6000) -> list[tuple[str, str]]:
    """渲染完整成绩单，返回若干条 (标题, 正文)。

    接口对正文长度有限制且没有公开数值，所以超过预算就按学期拆成多条推送——
    直接截断既会丢失内容，也可能产生不完整标签并被接口拒收。
    """
    by_term: dict[str, list] = {}
    for g in grades:
        by_term.setdefault(g.term or "未知学期", []).append(g)

    scored = sum(1 for g in grades if g.has_score)
    total_gpa = _weighted_gpa(grades)
    footer = (f'<div class="g">总加权平均绩点 <b class="gv">{total_gpa:.2f}</b></div>'
              if total_gpa is not None else "")

    tail = footer
    whole = "".join(_term_block(t, by_term[t]) for t in sorted(by_term)) + tail
    if len(_wrap(whole)) <= budget:
        return [(f"📋 当前成绩单（{scored}/{len(grades)} 门已出分）", _wrap(whole))]

    msgs = []
    terms = sorted(by_term)
    for i, term in enumerate(terms, 1):
        rows = by_term[term]
        body = _term_block(term, rows)
        if i == len(terms):
            body += tail
        n = sum(1 for g in rows if g.has_score)
        msgs.append((f"📋 成绩单 {term_label(term)}（{n}/{len(rows)} 门已出分）"
                     f"[{i}/{len(terms)}]",
                     _wrap(body)))
    return msgs


def build(cfg: dict) -> list[Notifier]:
    """按配置装配推送通道。

    返回 list 而不是单个对象：以后想同时推多个渠道时不用改调用方。
    enabled 为 true 但没填 token 时不加载——半残地启用只会让你以为
    推送开着，实际一直静默失败。
    """
    pp = cfg.get("pushplus") or {}
    if pp.get("enabled") and pp.get("token"):
        return [PushPlus(pp["token"])]
    return []
