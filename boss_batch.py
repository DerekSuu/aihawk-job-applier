"""BOSS 直聘批量定制打招呼循环（v2：应届岗硬过滤版）。

硬性过滤（任一命中即跳过，不发招呼）：
  1. 经验要求含年限（如 3-5年 / 5-10年 / 1-3年）→ 跳过。只接受：
     经验不限 / 无经验 / 在校生 / 应届生 / 未写年限的应届岗
  2. 标题含薪资（如 20-40K / 高薪 / 8千）或 高新/高薪/日结 → 判定骗子岗，跳过
  3. 标题含 经理/总监/资深/主管/专家 → 非应届岗，跳过
  4. 已打过招呼的岗位（greeted.json 持久化）→ 跳过

用法：
  .venv/Scripts/python.exe boss_batch.py --query "跨境支付" --city 101020100 --limit 5
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "profiles" / "stealth.json"
GREETED = ROOT / "greeted.json"

# 应届可投的经验写法（卡片文本中的经验字段）
OK_EXP = re.compile(r"经验不限|无经验|在校生|应届生|应届|在校|不限经验|未填写")
# 硬拒：3 年及以上经验（避开 2023年 这类年份：前后不能是数字）
HARD_EXP = re.compile(r"(?<!\d)[3-9]\s*[-~—至]\s*\d+\s*年|(?<!\d)[3-9]\s*年(以上|及)|三年以上|五年以上")
# 待核：1-3年——卡片可接受，但点进详情确认 JD 没提"需要工作经验"
MID_EXP = re.compile(r"(?<!\d)1\s*[-~—至]\s*3\s*年|(?<!\d)1\s*年以内|一年以内")
# 标题里的骗子特征
SCAM_TITLE = re.compile(r"\d+\s*[Kk](?![A-Za-z])|\d+万|高[薪新]|日结")
# 非应届管理岗（含英文职级）
SENIOR_TITLE = re.compile(r"经理|总监|资深|主管|专家|负责人|Director|Manager|KAM|VP|Leader|lead", re.I)
# 非正式全职（含日薪/按天结算岗——那是实习特征）
NOT_FULLTIME = re.compile(r"兼职|实习|\d+\s*元\s*/\s*天|元每天|日结|按天结算|\d+元/日")

# 招呼语来源：greeting.txt（由 AI 阅读用户简历后生成，含 {title} 占位符）。
# 仓库里不含任何个人经历——不同专业用户让 AI 按自己的简历生成即可。
_GREETING_FILE = ROOT / "greeting.txt"
GREETING_TEMPLATE = (
    _GREETING_FILE.read_text(encoding="utf-8").strip()
    if _GREETING_FILE.is_file()
    else "您好！我对贵司「{title}」岗位很感兴趣，"
         "希望能有机会进一步沟通，期待您的回复！"
)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _port() -> int:
    return json.loads(STATE.read_text(encoding="utf-8"))["port"]


def call(cmd: str, args: dict, timeout: float = 60) -> dict:
    body = json.dumps({"cmd": cmd, "args": args}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/cmd" % _port(), data=body,
        headers={"content-type": "application/json"})
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ev(expression: str, timeout: float = 45) -> str:
    r = call("eval", {"expression": expression}, timeout=timeout)
    if "error" in r:
        return "EVAL_ERROR: " + str(r["error"])[:120]
    return r.get("result", "")


def load_greeted() -> set:
    if GREETED.is_file():
        return set(json.loads(GREETED.read_text(encoding="utf-8")))
    return set()


def save_greeted(s: set) -> None:
    GREETED.write_text(json.dumps(sorted(s), ensure_ascii=False, indent=1),
                       encoding="utf-8")


def jd_requires_exp(jd: str) -> bool:
    """岗位详情 JD 是否提到需要工作经验（1-3年卡片须过此检查）。"""
    if re.search(r"\d+\s*年(以上|及)", jd):
        return True
    for m in re.finditer(r"(工作|相关|从业)经验", jd):
        ctx = jd[max(0, m.start() - 10):m.end() + 15]
        if "优先" in ctx:
            continue
        return True
    return False


def classify(text: str) -> tuple[str, str]:
    """卡片分级：ok=直接投 / jd_check=点进详情核 JD / reject=跳过。"""
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    if not parts or parts[0] == "查看更多信息":
        return "reject", "空卡片/UI元素"
    title = parts[0]
    if SCAM_TITLE.search(title):
        return "reject", "标题含薪资/高新(骗子岗)"
    if HARD_EXP.search(text):
        return "reject", "要求3年以上经验"
    if NOT_FULLTIME.search(text):
        return "reject", "兼职/实习岗"
    if OK_EXP.search(text) or re.search(r"应届|校招|管培|培训生|校园", text):
        return "ok", "应届友好"
    if MID_EXP.search(text) or HARD_EXP.search(text):
        return "jd_check", "卡片含年限,需核详情"
    # 卡片没写经验信息（BOSS 卡片常只有标题）→ 点进详情核查
    return "jd_check", "无经验信息,需核详情"


def get_cards() -> list[dict]:
    """读取搜索结果全部卡片文本，附索引。"""
    raw = ev(
        "JSON.stringify([...document.querySelectorAll('a[href*=\"job_detail\"]')]"
        ".filter(e=>e.offsetParent!==null).map((e,i)=>({i:i,"
        "t:e.innerText.trim().slice(0,120)})))"
    )
    try:
        return json.loads(raw)
    except Exception:
        return []


def click_card_by_text(title: str) -> str:
    # 用标题前 18 个字符做匹配锚点（卡片 innerText 以标题开头）
    anchor = title.replace("\\", "\\\\").replace("'", "\\'")[:18]
    return ev(
        "(()=>{var c=[...document.querySelectorAll('a[href*=\"job_detail\"]')]"
        ".find(e=>e.offsetParent!==null&&e.innerText.trim().startsWith('%s'));"
        "if(!c)return 'not-found';c.click();return 'clicked';})()" % anchor
    )


def js_click(text: str) -> str:
    return ev(
        "(()=>{var e=[...document.querySelectorAll('a,button')]"
        ".find(x=>x.offsetParent!==null&&x.innerText.trim()==='%s');"
        "if(!e)return 'not-found';e.click();return 'clicked';})()" % text
    )


def send_custom(title: str) -> tuple[bool, str]:
    """进聊天并发送按岗位定制的招呼语。"""
    time.sleep(4)
    if js_click("继续沟通") != "clicked":
        return False, "无继续沟通（可能已沟通过）"
    time.sleep(4)
    msg = GREETING_TEMPLATE.format(title=title)
    r = call("fill", {"sel": "#chat-input", "value": msg}, timeout=45)
    if "ok" not in r:
        return False, "fill失败"
    time.sleep(1)
    r = ev("(()=>{var b=document.querySelector('button.btn-send');"
           "if(!b)return 'no-btn';b.click();return 'sent';})()")
    if r != "sent":
        return False, "发送失败"

    # ── 发送验证钩子：必须证据确凿才算成功 ──
    # 用 text 命令（Playwright 协议级读取，不受页面 CSP 限制）：
    #   a) 输入框已清空  b) 定制消息出现在对话流里
    time.sleep(3)
    try:
        r1 = call("text", {"sel": "#chat-input", "limit": 30}, timeout=30)
        cleared = (r1.get("text", "").strip() == "")
        r2 = call("text", {"sel": "body", "limit": 8000}, timeout=30)
        in_thread = title[:10] in r2.get("text", "")
    except Exception as exc:
        return False, "验证读取失败:%s" % type(exc).__name__
    if cleared and in_thread:
        return True, "已发送(验证通过)"
    if not cleared:
        return False, "发送未确认(输入框仍有内容)"
    return False, "发送未确认(消息未出现在对话流)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="跨境支付")
    ap.add_argument("--city", default="101020100")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-gap", type=int, default=60)
    ap.add_argument("--max-gap", type=int, default=180)
    ns = ap.parse_args()

    greeted = load_greeted()
    from urllib.parse import quote
    url = "https://www.zhipin.com/web/geek/job?query=%s&city=%s" % (
        quote(ns.query), ns.city)

    r = call("navigate", {"url": url}, timeout=90)
    if "error" in r:
        sys.exit("导航失败: %s" % r["error"])
    time.sleep(12)
    if "security" in ev("location.href.slice(0,120)"):
        sys.exit("命中安全校验页——请人工完成后重跑。")

    cards = get_cards()
    print("搜索结果卡片数:", len(cards), flush=True)

    sent, log = 0, []
    for c in cards:
        if sent >= ns.limit:
            break
        text = c["t"]
        first_line = text.split("\n")[0][:36]
        key = first_line
        if key in greeted:
            print("  跳过(已打过):", first_line, flush=True)
            continue
        verdict, why = classify(text)
        if verdict == "reject":
            print("  跳过(%s): %s" % (why, first_line), flush=True)
            greeted.add(key)  # 不合格的也记录，避免重扫
            continue
        # 点卡片
        if click_card_by_text(first_line) != "clicked":
            print("  跳过(点击失败):", first_line, flush=True)
            continue
        time.sleep(4)
        if "security" in ev("location.href.slice(0,120)"):
            print("命中安全校验页，停止。已发 %d 条。" % sent)
            break
        # 详情核查：读右侧详情面板（含经验要求 + JD 全文）
        if verdict == "jd_check":
            time.sleep(3)
            r_det = call("text", {"sel": ".job-detail-box", "limit": 3000},
                         timeout=30)
            detail = r_det.get("text", "")
            if not detail:
                print("  跳过(详情读取失败):", first_line, flush=True)
                greeted.add(key)
                continue
            if HARD_EXP.search(detail) or jd_requires_exp(detail):
                print("  跳过(详情要求经验):", first_line, flush=True)
                greeted.add(key)
                continue
            # 详情通过：经验不限/1-3年/无硬性经验要求 → 继续沟通流程
            print("  详情核查通过(经验门槛OK):", first_line, flush=True)
        # 立即沟通（默认招呼自动发）
        if js_click("立即沟通") != "clicked":
            print("  跳过(无立即沟通):", first_line, flush=True)
            continue
        # 定制招呼补发
        ok2, why2 = send_custom(first_line)
        print("  %s: %s (%s)" % ("✅" if ok2 else "❌", first_line, why2),
              flush=True)
        log.append("%s %s %s" % ("OK" if ok2 else "FAIL", first_line, why2))
        greeted.add(key)
        save_greeted(greeted)
        if ok2:
            sent += 1
        # ── 强制随机间隔：无论成败，绝不连轴转 ──
        if sent < ns.limit:
            gap = random.randint(ns.min_gap, ns.max_gap)
            print("    休眠 %ds（随机间隔）..." % gap, flush=True)
            time.sleep(gap)
            # 失败后若疑似风控，间隔后复查再决定是否停
            if not ok2:
                if "security" in call("navigate", {"url": url}, timeout=90).get("url", ""):
                    print("疑似风控未解除，停止本轮。已发 %d 条。" % sent)
                    break
            call("navigate", {"url": url}, timeout=90)
            time.sleep(10)
            cards = get_cards()  # 刷新列表

    print("\n==== 本轮发送 %d 条 ====" % sent)
    ROOT.joinpath("boss_batch_log.txt").write_text(
        "\n".join(log), encoding="utf-8")


if __name__ == "__main__":
    main()
