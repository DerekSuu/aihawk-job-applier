"""只读侦察：核验一家企业的校招页面到底能不能用。

为什么单独有这个东西：targets.yaml 里大部分条目来自第三方聚合站，
那些站会写错截止日（实测科尔尼一家的截止日就差了三周，而且已经过期）。
在一条未经核实的 URL 上填表，等于把别人的笔误变成你投错的一次机会。

所以这里只做三件只读的事：打开页面、看标题与正文、判断是否需要登录。
不填、不点、不提交。结论写回 JSON，人工复核后才允许进入投递流程。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from .daemon import Browser

# 判断"这页跟校招有没有关系"的关键词。命中越多越可信。
CAMPUS_HINTS = (
    "校园招聘", "校招", "应届生", "管培生", "管理培训生", "2027届", "2027 届",
    "秋季招聘", "秋招", "campus", "graduate", "trainee",
)
# 判断"这轮已经结束了"的关键词。
CLOSED_HINTS = (
    "已截止", "已结束", "招聘结束", "网申已关闭", "报名已结束",
    "不在投递时间", "当前无可投递职位",
)


def probe(browser: Browser, url: str, wait_s: float = 6.0) -> dict:
    out: dict = {"url": url, "probed_at": datetime.now().isoformat(timespec="seconds")}

    # 现在的企业招聘站几乎全是 SPA：domcontentloaded 触发时正文还没渲染，
    # 实测三家站点的 body 只有 300-2500 字符，全是空壳。
    # 所以先等 networkidle（请求静默），失败再退到 load，最后才认命。
    nav = None
    for policy in ("networkidle", "load"):
        try:
            nav = browser.navigate(url, wait=policy, timeout=45000)
            if "error" not in nav:
                out["wait_policy"] = policy
                break
        except Exception:
            continue
    if nav is None or "error" in (nav or {}):
        out.update(ok=False, reason="导航失败：%s" % ((nav or {}).get("error") or "未知"))
        return out

    time.sleep(wait_s)  # 让前端把职位列表渲染出来再读

    out["final_url"] = browser.page.url
    out["title"] = (browser.page.title() or "")[:100]

    try:
        body = (browser.text("body", limit=6000)).get("text", "")
    except Exception:
        body = ""
    out["body_len"] = len(body)

    low = body.lower()
    out["campus_hits"] = sorted({h for h in CAMPUS_HINTS if h.lower() in low})
    out["closed_hits"] = sorted({h for h in CLOSED_HINTS if h in body})

    snap = browser.snapshot()
    rep = snap.get("report", {}) if isinstance(snap, dict) else {}
    out["login_state"] = rep.get("login")
    out["n_fields"] = len(rep.get("fields") or [])
    out["n_buttons"] = len(rep.get("buttons") or [])

    # 页面上出现的日期，作为"截止日"的候选证据。
    out["dates_found"] = sorted(set(re.findall(
        r"20\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}", body)))[:8]

    out["ok"] = True
    # 置信度是给人看的提示，不是自动放行：
    # 命中校招词且没有"已结束"字样，才算值得人去看一眼。
    out["verdict"] = (
        "已关闭" if out["closed_hits"]
        else "有校招信号" if out["campus_hits"]
        else "无明确校招信号"
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="只读核验校招页面")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--profile", default="profiles/recon")
    ap.add_argument("--out", default="recon.json")
    ap.add_argument("--delay", type=float, default=4.0,
                    help="两次请求之间的间隔，别把人家站点当靶子打")
    ap.add_argument("--headed", action="store_true")
    ns = ap.parse_args()

    browser = Browser(Path(ns.profile), headless=not ns.headed)
    browser.start()
    results = []
    try:
        for i, u in enumerate(ns.urls):
            r = probe(browser, u)
            results.append(r)
            print("[%d/%d] %-60s %s" % (
                i + 1, len(ns.urls), (r.get("title") or u)[:58], r.get("verdict", "?")),
                file=sys.stderr)
            if i < len(ns.urls) - 1:
                time.sleep(ns.delay)
    finally:
        browser.close()

    Path(ns.out).write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写入 %s" % ns.out)


if __name__ == "__main__":
    main()
