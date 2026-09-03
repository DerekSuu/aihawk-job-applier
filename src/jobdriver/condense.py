"""把一整页 DOM 压成"只有我真正需要看的那些字段"。

这是整套方案能不能跑长的唯一决定因素。

一页招聘表单的原始 DOM 常在 20K-50K token。一个岗位走 10-20 步，
不做压缩的话两三个岗位就会把一轮会话的上下文吃光，后面什么都干不了。
所以这里不返回 HTML，只返回一张结构化字段表：字段名、类型、是否必填、
当前值、可选值。单次结果压到 1-3K token。

判定"什么算字段"的逻辑集中在 _classify 里，站点适配的特殊规则走
site_hints 参数传入，不让通用逻辑去猜每家网站的怪癖。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 超过这个长度的选项列表不再逐条列出，只报数量。
# 省份下拉框有 34 项、行业分类有上百项，全列出来没有意义，
# 真要选的时候再单独问一次。
MAX_OPTIONS = 12

# 只保留这些标签作为"可交互元素"，其余一律丢弃。
INTERESTING = {"input", "select", "textarea", "button", "a"}

# 这些 type 没有填写意义。
SKIP_TYPES = {"hidden", "submit", "button", "image", "reset", "file"}


def _label_of(el: dict, doc_labels: dict[str, str]) -> str:
    """一个字段最像人话的那个名字。

    优先级：显式 <label for> > 包裹它的 <label> > aria-label >
    placeholder > name 属性 > id。表单里经常是三四种都没有，
    只能一路退到 id，所以这个函数是尽力而为而非保证。
    """
    eid = el.get("id") or ""
    if eid and eid in doc_labels:
        return doc_labels[eid]
    for key in ("ariaLabel", "placeholder", "name"):
        val = el.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()[:40]
    return eid or el.get("name") or "(unnamed)"


def _classify(el: dict) -> str | None:
    """这个元素是什么类型的输入。返回 None 表示不关心。"""
    tag = el.get("tag", "")
    if tag not in INTERESTING:
        return None

    typ = (el.get("type") or "").lower()

    if tag == "select":
        return "select"
    if tag == "textarea":
        return "textarea"
    if tag == "a":
        # 链接只在有文字时才有用，空链接（图标、装饰）不管。
        return "link" if (el.get("text") or "").strip() else None
    if tag == "button":
        return "button"
    if tag == "input":
        if typ in SKIP_TYPES:
            # file 单独放行：上传简历就是它，跳过的话整个流程断在这里。
            return "file" if typ == "file" else None
        if typ == "checkbox":
            return "checkbox"
        if typ == "radio":
            return "radio"
        return typ or "text"
    return None


def condense(raw: dict[str, Any]) -> dict[str, Any]:
    """把浏览器端抓来的原始元素表压成一份紧凑报告。

    入参是 daemon 在页面里 evaluate 出来的结构，不是 HTML 字符串——
    抽取动作在浏览器进程里做完，只把结论传回来，这是省 token 的第一道闸。
    """
    doc_labels: dict[str, str] = raw.get("labels") or {}
    elements = raw.get("elements") or []

    fields: list[dict] = []
    buttons: list[dict] = []
    links: list[dict] = []
    files: list[dict] = []

    for el in elements:
        kind = _classify(el)
        if kind is None:
            continue

        sel = el.get("selector") or ""
        label = _label_of(el, doc_labels)
        required = bool(el.get("required"))

        if kind == "file":
            files.append({"sel": sel, "label": label, "required": required})
            continue

        if kind in ("button", "link"):
            text = (el.get("text") or "").strip()[:40]
            if not text:
                continue
            entry = {"sel": sel, "text": text}
            (buttons if kind == "button" else links).append(entry)
            continue

        field: dict[str, Any] = {
            "sel": sel,
            "label": label,
            "kind": kind,
            "req": required,
        }
        if kind in ("select", "radio", "checkbox"):
            opts = el.get("options") or []
            if len(opts) > MAX_OPTIONS:
                field["opts"] = f"<{len(opts)} 项，按需单独拉取>"
            elif opts:
                field["opts"] = [str(o)[:24] for o in opts]
        if kind == "checkbox":
            field["checked"] = bool(el.get("checked"))
        else:
            cur = el.get("value")
            if cur:
                field["cur"] = str(cur)[:60]
        fields.append(field)

    out: dict[str, Any] = {
        "url": raw.get("url", ""),
        "title": (raw.get("title") or "")[:80],
        "fields": fields,
        "files": files,
        "buttons": buttons[:25],
    }
    # 链接最容易爆炸：一个招聘站导航区能有上百个。默认不返回，
    # 需要的时候用 links 子命令单独拉，避免每次快照都付这笔钱。
    if raw.get("want_links"):
        out["links"] = links[:40]

    meta = raw.get("login_state")
    if meta:
        out["login"] = meta

    return out


def render(report: dict[str, Any]) -> str:
    """把报告渲染成给我看的紧凑文本。

    用固定宽度的短行而不是 JSON：JSON 的花括号和引号在这里是纯浪费，
    而一行一个字段的排版我能直接数出还有几个空要填。
    """
    lines: list[str] = []
    lines.append("URL   %s" % report.get("url", ""))
    title = report.get("title")
    if title:
        lines.append("TITLE %s" % title)

    login = report.get("login")
    if login:
        lines.append("LOGIN %s" % login)

    fields = report.get("fields") or []
    if fields:
        lines.append("")
        lines.append("-- 待填字段 %d --" % len(fields))
        for f in fields:
            flag = "*" if f.get("req") else " "
            cur = f.get("cur")
            curtxt = (' 现值="%s"' % cur) if cur else ""
            opttxt = ""
            if "opts" in f:
                o = f["opts"]
                opttxt = "  选项=%s" % (o if isinstance(o, str) else "/".join(o))
            lines.append(
                "%s%-3s %-22s %s%s%s"
                % (flag, f.get("kind", "?"), (f.get("label") or "")[:22],
                   f.get("sel", "")[:38], curtxt, opttxt)
            )

    files = report.get("files") or []
    if files:
        lines.append("")
        lines.append("-- 上传项 --")
        for f in files:
            lines.append("%s  %s" % (f.get("label"), f.get("sel")))

    buttons = report.get("buttons") or []
    if buttons:
        lines.append("")
        lines.append("-- 按钮 --")
        for b in buttons:
            lines.append("%-30s %s" % ((b.get("text") or "")[:30], b.get("sel")))

    return "\n".join(lines)


def summarize(raw: dict[str, Any]) -> str:
    return render(condense(raw))


if __name__ == "__main__":
    import sys
    print(json.dumps(condense(json.load(open(sys.argv[1], encoding="utf-8"))),
                     ensure_ascii=False, indent=1))
