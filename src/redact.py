"""把凭据从要外传的文本里抹掉。

日志会被 journalctl 翻、被截图、被贴进聊天框；异常文本还会随告警推给
PushPlus。凡是这两条路上的 URL 都得先过一遍这里。
"""
from __future__ import annotations

import re

# CAS ticket、Tomcat 会话、成绩页访问口令。
# 值的边界取 ? & ; 和空白：URL 里 jsessionid 挂在路径上（;jsessionid=...），
# 后面还可能跟着查询串，用一条模式统一处理，免得两条模式互相把对方的
# 分隔符当成值吃掉。
_CREDENTIAL_PARAMS = ("ticket", "jsessionid", "token")
_PATTERN = re.compile(
    r"(?i)(" + "|".join(_CREDENTIAL_PARAMS) + r")=[^?&;\s]+")


def redact_url(text: str | None) -> str | None:
    """抹掉 URL 里的凭据，保留结构好让人还能看出哪儿出了错。"""
    if not text:
        return text
    return _PATTERN.sub(r"\1=<已隐去>", text)
