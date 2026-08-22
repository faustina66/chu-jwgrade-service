"""推送样例集：预览项目支持的各种消息样式。

    python tools/demo_push.py                         只生成 HTML 预览，不推（默认）
    python tools/demo_push.py --list                  列出全部样例
    python tools/demo_push.py --push --only 1         只推第 1 条测试消息
    python tools/demo_push.py --only 3 --out /tmp/x.html 只预览第 3 条

`--push` 必须配合 `--only N`，避免新用户一次误发全部样例。

**全程不访问教务系统**：使用虚构数据，只调用 PushPlus。可用于确认显示样式，
或在首次部署后查看推送效果。

样例覆盖长安大学可能出现的四类变化，并分别提供正常成绩和不及格成绩：

    新增成绩 / 成绩变更 / 成绩被撤回 / 成绩重新发布

第五种 `filled`（成绩已发布）未列入样例：它适用于课程行已经存在、之后才填写
分数栏的页面。长安大学目前表现为整行出现或消失，因此暂不需要该样例。

`--push` 需要 PushPlus token。服务器上不要直接使用 `sudo -u`（无法读取
`/etc/jwgrade.env`），可参照 `deploy/setup.sh` 的部署检查方式使用 systemd-run。
"""
from __future__ import annotations

import argparse
import html as _html
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src import config, notifier  # noqa: E402
from src.models import Change, Grade  # noqa: E402

LF = chr(10)
T2 = "2025-2026 2"


def _g(name, code, seq, final, gpa, mid, fin, usual, overall,
       lab="", cat="学科基础课程（2022）", credit="5") -> Grade:
    return Grade(
        term=T2, course_id=code, course_name=name, score=final,
        credit=credit, gpa=gpa,
        raw={"学年学期": T2, "课程代码": code, "课程序号": seq,
             "课程名称": name, "课程类别": cat, "学分": credit,
             "期中成绩": mid, "期末成绩": fin, "平时成绩": usual,
             "总评成绩": overall, "实验成绩": lab, "最终": final, "绩点": gpa})


# 满分：各项 100，绩点 5。挂科：最终 45，绩点 0。同一门课，好让对照干净。
PERFECT = _g("高等数学Ⅱ（二）", "12XK1102", "12XK1102.03",
             "100", "5", "100", "100", "100", "100", lab="100")
FAILED = _g("高等数学Ⅱ（二）", "12XK1102", "12XK1102.03",
            "45", "0", "40", "32", "60", "45")

# 三科依次出分用的。**一律满分**——样例里除了那条挂科，不放任何真实分数：
# 该脚本生成的内容可能用于截图或分享，因此只使用虚构成绩。
S1 = _g("高等数学Ⅱ（二）", "12XK1102", "12XK1102.03",
        "100", "5", "100", "100", "100", "100")
S2 = _g("大学物理Ⅱ（一）", "12XK1203", "12XK1203.13",
        "100", "5", "100", "100", "100", "100", lab="100", credit="3")
S3 = _g("C语言程序设计", "24XK1706", "24XK1706.05",
        "100", "5", "100", "100", "100", "100", credit="2")

# (说明, [Change...])。列表长度 > 1 的那条会合并成一条消息。
SAMPLES: list[tuple[str, list[Change]]] = [
    ("满分 · 新增成绩", [Change("new", PERFECT)]),
    ("满分 · 成绩变更（60 → 100）", [Change("changed", PERFECT, old_score="60")]),
    ("满分 · 成绩被撤回", [Change("withdrawn", PERFECT, old_score="100")]),
    ("满分 · 成绩重新发布", [Change("republished", PERFECT, old_score="100")]),

    ("挂科 · 新增成绩", [Change("new", FAILED)]),
    ("挂科 · 成绩变更（100 → 45）", [Change("changed", FAILED, old_score="100")]),
    ("挂科 · 成绩被撤回", [Change("withdrawn", FAILED, old_score="45")]),
    ("挂科 · 成绩重新发布", [Change("republished", FAILED, old_score="45")]),
    ("依次出分 · 第 1 科", [Change("new", S1)]),
    ("依次出分 · 第 2 科", [Change("new", S2)]),
    ("依次出分 · 第 3 科", [Change("new", S3)]),
    ("三科落在同一轮 · 合并成一条",
     [Change("new", S1), Change("new", S2), Change("new", S3)]),
]

PAGE_CSS = (
    "<style>"
    "body{margin:0;padding:16px;background:#e9ebef;"
    "font-family:-apple-system,'PingFang SC',sans-serif}"
    ".px-wrap{max-width:460px;margin:0 auto}"
    ".px-h1{font-size:18px;margin:0 0 6px;color:#111}"
    ".px-sub{font-size:13px;color:#666;line-height:1.7;margin:0 0 18px}"
    ".px-ct{font-size:14px;font-weight:700;color:#333;margin:22px 0 4px}"
    ".px-t{font-size:12px;color:#1156c4;font-weight:700;margin:0 0 6px;"
    "word-break:break-all}"
    ".px-card{background:#fff;border-radius:10px;padding:6px 16px 12px}"
    ".px-m{font-size:11px;color:#999;margin:4px 0 0}"
    "</style>")


# 推失败时给的排查提示。PushPlus 的 code=999 至少对应三件事，而且返回的
# msg 通常只显示「服务端验证错误」，因此这里补充常见原因和处理方向。
HINT = """      这一条没推成功。PushPlus 的 code=999 常见三种原因：
        - 刚推过一模一样的内容 —— 换一条，或者隔一阵子再来
        - 推太快，免费额度约每分钟 5 条 —— 加大 --gap
        - 正文太长
      真正的原因看 pushplus.plus 里账号的发送记录。"""


def render_all(level: str = "full") -> list[tuple[str, str, str]]:
    """返回 [(样例说明, 推送标题, HTML 正文)]。"""
    return [(desc, *notifier.render(changes, level)) for desc, changes in SAMPLES]


def write_preview(rows, out: Path) -> Path:
    parts = ["<!doctype html><meta charset=utf-8>",
             '<meta name=viewport content="width=device-width,initial-scale=1">',
             "<title>推送样例集</title>", PAGE_CSS,
             '<div class="px-wrap">',
             '<div class="px-h1">推送样例集</div>',
             '<div class="px-sub">长安大学会发生的四种消息，'
             "满分和挂科各来一遍；最后是三科依次出分和合并的对照。<br>"
             "蓝字是推送标题，白卡是 render() 的原样输出。<b>数据是编的，"
             "不访问教务系统。</b></div>"]
    for i, (desc, title, body) in enumerate(rows, 1):
        parts.append(f'<div class="px-ct">{i}. {_html.escape(desc)}</div>')
        parts.append(f'<div class="px-t">{_html.escape(title)}</div>')
        parts.append(f'<div class="px-card">{body}</div>')
        parts.append(f'<div class="px-m">正文 {len(body)} 字符</div>')
    parts.append("</div>")
    out.write_text("".join(parts), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="推送样例集（不访问教务系统）")
    ap.add_argument("--config", default=str(REPO / "config.yaml"))
    ap.add_argument("--push", action="store_true", help="真的推到微信")
    ap.add_argument("--only", type=int, metavar="N", help="只处理第 N 条")
    ap.add_argument("--list", action="store_true", help="只列样例，不渲染")
    ap.add_argument("--out", default=str(REPO / "preview-demo-push.html"))
    ap.add_argument("--gap", type=float, default=15.0,
                    help="逐条推送之间的间隔秒数，默认 15（PushPlus 限流）")
    args = ap.parse_args()

    if args.push and args.only is None:
        print("为了避免误发多条测试消息，--push 必须同时指定 --only N。"
              "先用 --list 查看样例编号。")
        return 2

    if args.list:
        for i, (desc, changes) in enumerate(SAMPLES, 1):
            n = len(changes)
            print(f"{i:3}. {desc}" + (f"（{n} 门合并）" if n > 1 else ""))
        return 0

    # 渲染既不要 token 也不要配置。只看样式的时候没有 config.yaml 也该能跑——
    # 开发机上通常就没有（它在 .gitignore 里），而"先看 HTML 再决定推不推"
    # 正是这个脚本最常用的方式。
    cfg: dict = {}
    try:
        cfg = config.load(args.config, require_push=args.push)
    except FileNotFoundError:
        if args.push:
            raise
        print(f"没找到 {args.config}，按默认的 full 渲染（不影响预览）")
    level = cfg.get("notify", {}).get("detail_level", "full")
    rows = render_all(level)

    if args.only is not None:
        if not 1 <= args.only <= len(rows):
            print(f"--only 要在 1~{len(rows)} 之间")
            return 2
        rows = [rows[args.only - 1]]

    out = write_preview(rows, Path(args.out))
    print(f"详略级别：{level}　样例 {len(rows)} 条")
    print(f"预览已写到：{out}")

    if not args.push:
        print(LF + "没有 --push，什么都没推。确认样式后加 --push 再跑一次。")
        return 0

    chans = notifier.build(cfg.get("notify", {}))
    if not chans:
        print("没有可用的推送通道，检查 notify.pushplus")
        return 2

    failed = []
    for i, (desc, title, body) in enumerate(rows, 1):
        # 标题里有 emoji，Windows 控制台是 GBK，直接 print 会自己抛
        # UnicodeEncodeError——报错通道坏掉是最难查的一类。
        safe = title.encode(sys.stdout.encoding or "utf-8", "replace").decode(
            sys.stdout.encoding or "utf-8")
        print(f"[{i}/{len(rows)}] {desc} —— {safe}")
        sent = all(c.send(title, body) for c in chans)
        if not sent:
            if not failed:
                # 只在第一条失败时说全，后面重复刷屏没意义
                print(HINT)
            else:
                print("      这一条也没推成功")
            failed.append(i)
        if i < len(rows) and args.gap > 0:
            # 2026-08-19 实测：PushPlus 免费额度约**每分钟 5 条**，超了就回
            # code=999「服务端验证错误」——这个码很误导人，它和内容无关。
            # 当时 --gap 3（20 条/分钟），第 1~5 条成功、6~10 全废、到第 60 秒
            # 窗口翻篇后 11、12 又成功。默认给 15 秒 = 4 条/分钟，留一点余量。
            #
            # 守护进程通常不会触发此限制：每轮只发送一条，两轮间隔 30 分钟；即使触发也有
            # 发件箱兜着，下一轮补发时窗口早过了。
            time.sleep(args.gap)

    if failed:
        print(f"{LF}{len(rows) - len(failed)} 条成功，{len(failed)} 条失败："
              + "、".join(f"第 {n} 条" for n in failed))
        return 1
    print(f"{LF}{len(rows)} 条全部送达")
    return 0


if __name__ == "__main__":
    sys.exit(main())
