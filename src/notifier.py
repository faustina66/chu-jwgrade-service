"""微信推送。

只保留 PushPlus 一个通道。要加别的（Server酱 / ntfy / 邮件）就继承
Notifier 实现一个 send()，再在 build() 里认一下配置，十几行的事。

正文用 HTML 而不是 markdown：微信消息详情页本质是 webview，能渲染真 HTML。
markdown 在手机上排版很糟——全角空格对不齐、长行乱折、绩点会被断成两行。
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
    """接口明确拒收，本轮别再试。

    和"网络抖了一下"不是一回事，重试要么没用要么有害：
      没用  token 错了，它不会在六秒内变对
      有害  已经被判定"请求过多"，接着发只会让限制更久

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
        """推送失败要重试——出分通知漏掉一条比什么都糟。

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
                # 故意不带 (n/3)：那个格式看着像网络抖动，会让人"再等等看"，
                # 而这条要的是让你一眼知道"重试没用，去改配置"。
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

    # 这几个码重试没用甚至有害。值会原样进日志和告警，所以写成"该干什么"。
    # 官方返回码表：pushplus.plus/doc/guide/code.html
    FATAL: ClassVar[dict[str, str]] = {
        "900": "账号被判定请求过多而受限。**别接着发**——官方明确说可以据此"
               "判断是否还该继续调用。等它自己解除，发件箱下一轮再补。",
        "903": "token 无效（不是额度用完，那是 900）。去 pushplus.plus 重新"
               "复制 token，写进 /etc/jwgrade.env 的 PUSHPLUS_TOKEN，重启服务。"
               "等多久都不会自己好。",
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
            # 而那是从 send() 里窜出去的异常，不是"返回 False"——
            # 调用方拿不到"推送失败"这个结论，整轮就崩了。
            return f"响应不是对象：{str(data)[:200]}"
        code = str(data.get("code"))
        if code in self.FATAL:
            # 2026-08-19 之前 903 在注释里被写成「日额度」，那是推的不是查的，
            # 方向正好反了。查官方返回码表才纠正过来，顺带发现 900 更要紧。
            raise PushRejected(f"code={code} —— {self.FATAL[code]}")
        if code != "200":
            #   999  「服务端验证错误」—— 通用错误码，**至少对应三件事**，
            #        2026-08-19 被它绕了两回：
            #        a) 推太快。免费额度约每分钟 5 条：3 秒一条连推 12 条，
            #           1~5 成功、6~10 全回 999、第 60 秒窗口翻篇后 11、12
            #           又成功。特征是连续几条一起失败。
            #        b) 同一条消息重复推。15:03 推成功，15:25 原样再推被拒，
            #           15:29 换一条内容立刻成功。特征是只有那一条失败。
            #        c) 正文太长（27 门课用 inline 样式那次）。
            #        看到 999 的排查顺序：先想"是不是刚推过一模一样的"，
            #        再想频率，最后才怀疑内容。
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
    # 2026-08-17 实测又加一条：**小程序只认类选择器**。带类型选择器的规则要么
    # 被整条忽略，要么降解到最外层的类上——曾经 `.jw p b{color:蓝}` 把整段文字
    # 都染成了蓝色，而那条规则本意只想染数值。所以颜色一律挂在裸类上，
    # 连 `.jw ` 前缀都不加。
    #
    # 去掉 `.jw ` 前缀是有代价的，写在这儿免得后人以为是疏忽：这些规则现在
    # 作用于 PushPlus 的**整个页面**，不只我们这段 HTML；而 .l .e .n .c .s
    # 这些类名短到很容易和宿主页面撞，两个方向都可能（我们染到它，它也可能
    # 盖掉我们）。2026-08-17 实测公众号和小程序两个渲染器都正常，所以维持
    # 现状；哪天出现串色，第一嫌疑就是这里，改法是给类名加前缀
    # （.jwl / .jwe …），代价是每门课多十几字节。
    #
    # 2026-08-19 真机推翻了一个旧判断。原来写的是「`b{}` 是有意保留的例外：
    # 公众号认它，小程序忽略它，**代价为零**」——代价不是零。小程序里那些
    # 蓝色数字全变成了黑的，只有「空」（`.e`，是个类）还蓝着，一张卡看着
    # 半生不熟。**推理说"没人看得出来"，真机一测就现了原形。**
    #
    # 所以数值改挂 `.v`。每门课多约 64 字节，一条消息能装的门数从 10 掉到 9
    # ——正常出分一轮就一两门，够不着。`b{}` 那条留着当兜底，一次性开销，
    # 万一哪天漏了个没写 class 的 <b> 至少在公众号里还是蓝的。
    "<style>"
    # word-break 这两条是 2026-08-19 加的，起因是手机上「最终100」被劈成
    # 「最终10」换行「0」。原因不在我们：PushPlus 的页面给容器设了
    # word-break:break-all（任意两字符之间都能断），而我们的类选择器全是裸的，
    # 宿主样式盖得到我们。iframe 隔离复现过：break-all 下 260px 宽必劈数字，
    # normal 不劈但会把「平时」拆成两半，keep-all 两样都不发生。
    #
    # .jw 上写 normal 是**把宿主顶回去**；明细行再用 .d 升到 keep-all——
    # 那两行每一项之间都有 " · " 可断，不怕断不开。标题行故意留在 normal：
    # 「交通工程专业新生研讨课」这种没标点的长名字，keep-all 会整块撑出去。
    ".jw{font-family:-apple-system,sans-serif;font-size:15px;line-height:1.5;color:#000;word-break:normal}"
    ".d{word-break:keep-all}"
    # 两张表都是「第一列 40%，其余平分」：明细表的期末/实验落在 40%，
    # 和上面那张两列表的分数、课程类别、绩点一条线；平时/最终被推到 70%。
    # 4:6 而不是五五开——课程序号是定长的十来个字符，课程类别可能很长。
    # 不写 border-collapse：小程序对它的支持没验过，我们本来也没边框。
    # ⚠️ 这三条的类名**故意不带引号**（`<table class=t>`），全文仅此一处。
    #
    # HTML5 允许属性值不加引号，浏览器照常匹配，拿到满宽、列宽 4:6、按列对齐；
    # 而小程序的解析器认死引号，匹配不上这个类 —— 于是**一条表格 CSS 都不生效**，
    # 它用自己的默认表格渲染，那个渲染本来就是满宽自动列，一样好看。
    #
    # 2026-08-19 为这事绕了一大圈：先给表格补引号（`.t` 生效 → 列被压死）、
    # 再内联 width（没用，破坏源不在缺 width）、再改成 `.jw td` 后代选择器
    # （更糟，小程序连后代选择器也认，第一列撑到 40% 直接顶出屏幕）。三轮
    # 之后回头看最早那版才发现：**它本来就是对的，不带引号就是那道墙。**
    #
    # 所以别"顺手"给这几个补引号。数值那边的 class="v" 必须带引号——两件事
    # 方向相反，一个要让小程序看见，一个要让它看不见。
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
    # 宁可整组换到下一行，也别出现「60 →」换行「100」。
    ".w{color:#c5221f;font-weight:700;white-space:nowrap}"
    # 改完及格用的蓝色版「旧 → 新」。和 .w 只差颜色，字号故意不写死，
    # 两者必须一样大——否则同一个位置的字忽大忽小。
    ".wb{color:#1156c4;font-weight:700;white-space:nowrap}"
    ".ws{color:#000;font-weight:400}"
    ".g{margin:16px 0 0;padding:10px;background:#eef2f8;border-radius:6px;"
    "text-align:center;font-size:16px;color:#000}"
    ".gv{color:#1156c4;font-weight:700;font-size:20px}"
    # 变化类型那一行。挂在 div 上，不是 p —— 见 _kind_line() 里的说明。
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

    从中间切会留下残缺标签，接口直接判为非法内容拒收——那比少几门课糟得多。
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

    挂科时整行转红，**连"空"一起**：这门课整体是坏消息，留一半蓝的会让人
    误以为其中几项还算好看。
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
    # <table> 标签一起砍——那六项会糊成一坨。公众号那边实测没问题。
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
    # 撤回是坏消息，别被裹在"出分啦"里推送出去
    if len(withdrawals) == len(changes):
        if len(changes) == 1:
            return f"⚠️ {changes[0].grade.course_name} 成绩被撤回"
        return f"⚠️ {len(changes)} 门成绩被撤回"
    if len(changes) == 1:
        c = changes[0]
        # 标题里不放分数。PushPlus 的消息详情页给标题也设了
        # word-break:break-all，「出分了：100」会被劈成「出分了：10」换行「0」，
        # 而标题是它自己渲染的，我们的 CSS 够不着。
        # 代价：锁屏上只看得到"哪门课出分了"，分数要点进去看。
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

    红色只留给真的坏消息：撤回，以及**改完之后没及格**的变更。
    改分本身不分好坏——补考涨上来也是"成绩变更"，那种再标红就是虚惊一场。
    """
    bad = c.kind == "withdrawn" or (c.kind == "changed" and _is_failing(c.grade))
    return f'<div class="{"kw" if bad else "k"}">{_esc(c.label)}</div>'


def _headline(c: Change, brief: bool = False) -> str:
    """课程名一行，[课程序号] 和分数另起一行。

    课程名长短不一，挤在一起时这台手机断得开、那台断不开，位置全看屏幕。
    拆开就固定了，而且分数能和下一行的课程类别对齐（同在第 2 列）。

    brief=True 时只留课程名和分数，标识表整个不输出——学期、课程序号、
    课程类别、学分、绩点一个都不给。那正是 brief 存在的理由：**少给第三方
    推送服务看东西**。2026-08-20 之前这里不看 level，于是那五个字段照样
    发了出去，而配置注释还写着「只有课程名和分数」。
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
            # 真正把这句话兑现的是 _headline(brief=...)——2026-08-20 之前
            # 这条注释在，实现不在。
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
    截断是最糟的选择：既丢内容，又容易切出残缺标签让接口直接拒收。
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
