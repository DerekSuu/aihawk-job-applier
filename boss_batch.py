"""BOSS 直聘批量定制打招呼循环（通用版：规则全部走配置，代码不含个人硬约束）。

个人筛选规则放在 `filter_rules.yaml`（不入库）。首次使用请先让 AI 按
`prompts/grill-job-filters.md` 对你做一次「岗位要求 grilling」，自动生成该文件；
也可照着 `filter_rules.example.yaml` 手改。

配置结构：
  reject.*   一票否决（命中即跳过）
  require.*  正向白名单（留空 = 不限）
  salary.*   月薪下限与「薪资不明」策略

机制（与个人偏好无关，内置）：
  · 列表扫完自动刷新搜索（--max-refresh 轮）
  · 发送前 `gate_check` 把全部规则独立复检一遍，任一不过放弃发送
  · 发送后验证钩子（输入框清空 + 消息出现在对话流）
  · 风控：失败后复查 security 页命中即停；连续 N 次失败熔断（--max-fail）
  · greeted.json 持久化去重

用法：
  .venv/Scripts/python.exe boss_batch.py --query "商业分析" --limit 10
  .venv/Scripts/python.exe boss_batch.py --rules my_rules.yaml --query "支付" --overseas
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
RULES_FILE = ROOT / "filter_rules.yaml"
NL = "\n"

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# ─────────────── 通用解析式（与个人偏好无关，仅用于识别客观特征）───────────────

# 骗子岗特征：标题直接挂薪资数字
SCAM_TITLE = re.compile(r"\d+\s*[Kk](?![A-Za-z])|\d+万|高[薪新]|日结")
# 电话销售类
PHONE_SALES = re.compile(r"电话销售|电销|外呼|呼叫中心|电话客服|电话营销|坐席")
# 非正式全职
NOT_FULLTIME = re.compile(r"兼职|实习|\d+\s*元\s*/\s*天|元每天|日结|按天结算|\d+元/日")
# 技术岗（默认不启用，由配置 reject.dev_keywords 决定）
# 应届友好写法（用于经验门槛判断）

SALARY_FLOOR_DEFAULT = 0

DEFAULT_RULES = {
    "reject": {
        "title_keywords": [],
        "employer_keywords": [],
        "jd_keywords": [],
        "industries": [],
        "required_majors": [],
        "dev_keywords": [],
        "phone_sales": False,
        "part_time": False,
        "scam_title": True,
        "min_years_experience": 0,
        "experience_required": False,
    },
    "require": {
        "industries": [],
        "functions": [],
        "allowed_majors": [],
        "overseas": False,
    },
    "salary": {
        "floor": SALARY_FLOOR_DEFAULT,
        "unknown": "allow",           # allow | reject
    },
}


# ───────────────────────────── 配置装载与编译 ─────────────────────────────

def _merge(base: dict, over: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load_rules(path: Path) -> dict:
    if yaml is None:
        sys.exit("缺少 PyYAML：请先 pip install pyyaml")
    if not path.is_file():
        sys.exit(
            "未找到规则文件 %s。\n"
            "请先让 AI 按 prompts/grill-job-filters.md 做一次「岗位要求 grilling」生成它，\n"
            "或复制 filter_rules.example.yaml 为 filter_rules.yaml 后手动填写。" % path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _merge(DEFAULT_RULES, data)


def _re_from(terms) -> re.Pattern | None:
    """把关键词列表编译成正则。条目按**正则**处理（可用 `电商(?!支付)` 这类断言）。"""
    terms = [str(t) for t in (terms or []) if str(t).strip()]
    if not terms:
        return None
    try:
        return re.compile("|".join(terms))
    except re.error as exc:
        sys.exit("filter_rules 里的关键词无法编译为正则：%s\n  条目：%s" % (exc, terms))


def _years_re(n: int) -> re.Pattern | None:
    """构造「要求 ≥ n 年经验」的正则（n<=0 表示不启用）。"""
    if not n or n <= 0:
        return None
    hi = r"[%d-9]" % n if n < 9 else r"9"
    two = r"\d{2,}"
    return re.compile(
        r"(?<!\d)(?:%s|%s)\s*年(以上|及以上|起)|(?<!\d)(?:%s|%s)\s*[-~—至]\s*\d+\s*年"
        % (hi, two, hi, two))


class Rules:
    """把 filter_rules.yaml 编译成一组可直接调用的判定。"""

    def __init__(self, cfg: dict):
        rj, rq, sa = cfg["reject"], cfg["require"], cfg["salary"]
        self.title_kw = _re_from(rj.get("title_keywords"))
        self.employer_kw = _re_from(rj.get("employer_keywords"))
        self.jd_kw = _re_from(rj.get("jd_keywords"))
        self.industry_black = _re_from(rj.get("industries"))
        self.majors_required = _re_from(rj.get("required_majors"))
        self.dev_kw = _re_from(rj.get("dev_keywords"))
        self.phone_sales = bool(rj.get("phone_sales"))
        self.part_time = bool(rj.get("part_time"))
        self.scam_title = bool(rj.get("scam_title"))
        self.years_re = _years_re(int(rj.get("min_years_experience") or 0))
        self.experience_required = bool(rj.get("experience_required"))
        self.industries = _re_from(rq.get("industries"))
        self.functions = _re_from(rq.get("functions"))
        self.majors_allowed = _re_from(rq.get("allowed_majors"))
        self.overseas = bool(rq.get("overseas"))
        self.salary_floor = int(sa.get("floor") or 0)
        self.salary_unknown = str(sa.get("unknown") or "allow").lower()

    # ── 一票否决 ──
    def card_reject(self, card: str, title: str) -> tuple:
        if self.scam_title and SCAM_TITLE.search(title):
            return "标题含薪资(骗子岗)"
        if self.phone_sales and PHONE_SALES.search(card):
            return "电话销售岗"
        if self.part_time and NOT_FULLTIME.search(card):
            return "兼职/实习岗"
        if self.title_kw and self.title_kw.search(title):
            return "标题命中排除词"
        if self.employer_kw and self.employer_kw.search(card):
            return "雇主人名命中排除词"
        if self.jd_kw and self.jd_kw.search(card):
            return "卡片命中排除词"
        if self.industry_black and self.industry_black.search(card):
            return "黑名单行业(雇主)"
        if self.dev_kw and self.dev_kw.search(title):
            return "技术岗"
        if self.years_re and self.years_re.search(card):
            return "要求经验年限"
        return ""

    # ── 正向白名单 ──
    def industry_ok(self, card: str, detail: str) -> tuple:
        if self.industry_black and self.industry_black.search(card):
            return False, "黑名单行业(雇主)"
        if not self.industries:
            return True, "行业不限"
        blob = card + " " + (detail or "")
        if self.industries.search(blob):
            return True, "行业命中白名单"
        return False, "行业不在白名单(精确优先→拒)"

    def function_ok(self, title: str) -> tuple:
        if not self.functions:
            return True, "职能不限"
        if self.functions.search(title):
            return True, "职能命中白名单"
        return False, "职能不在白名单"

    def major_ok(self, detail: str) -> tuple:
        if not self.majors_required:
            return True, "无专业限制"
        if self.majors_allowed and self.majors_allowed.search(detail):
            return True, "专业在白名单"
        if self.majors_required.search(detail):
            return False, "仅列受限专业(拒)"
        return True, "未提受限专业"

    def salary_ok(self, detail: str) -> tuple:
        lo = self._parse_salary_floor(detail)
        if lo is None:
            if self.salary_unknown == "reject":
                return False, "薪资不明(拒)"
            return True, "薪资不明(放)"
        if self.salary_floor and lo < self.salary_floor:
            return False, "月薪下限低于%d" % self.salary_floor
        return True, "薪资OK"

    @staticmethod
    def _parse_salary_floor(detail: str):
        head = (detail or "")[:600]
        m = re.search(r"(\d+(?:\.\d+)?)\s*[-~—至]\s*(\d+(?:\.\d+)?)\s*[Kk]", head)
        if m:
            return float(m.group(1)) * 1000
        m = re.search(r"(\d+(?:\.\d+)?)\s*[Kk]\s*(?:以上|起|\+)", head)
        if m:
            return float(m.group(1)) * 1000
        m = re.search(r"(\d+(?:\.\d+)?)\s*[-~—至]\s*(\d+(?:\.\d+)?)\s*万", head)
        if m:
            return float(m.group(1)) * 10000
        m = re.search(r"(\d+(?:\.\d+)?)\s*万\s*(?:以上|起)", head)
        if m:
            return float(m.group(1)) * 10000
        m = re.search(r"(\d+(?:\.\d+)?)\s*千", head)
        if m:
            return float(m.group(1)) * 1000
        return None

    def jd_requires_exp(self, jd: str) -> bool:
        if not self.experience_required:
            return False
        if self.years_re and self.years_re.search(jd):
            return True
        for m in re.finditer(r"(工作|相关|从业)经验", jd or ""):
            ctx = jd[max(0, m.start() - 10):m.end() + 15]
            if "优先" in ctx:
                continue
            return True
        return False

    def detail_reject(self, card: str, title: str, detail: str) -> str:
        """详情层复检：行业/职能/专业/薪资/经验。通过返回空串。"""
        for ok, why in (self.industry_ok(card, detail),
                        self.function_ok(title),
                        self.major_ok(detail),
                        self.salary_ok(detail)):
            if not ok:
                return why
        if self.years_re and self.years_re.search(detail):
            return "详情要求年限"
        if self.jd_requires_exp(detail):
            return "详情要求经验"
        return ""

    # ── 发送前独立校对 ──
    def gate(self, card: str, title: str, detail: str, overseas: bool = False) -> tuple:
        fails = []
        r = self.card_reject(card, title)
        if r:
            fails.append(r)
        for ok, why in (self.industry_ok(card, detail),
                        self.function_ok(title),
                        self.major_ok(detail),
                        self.salary_ok(detail)):
            if not ok:
                fails.append(why)
        if self.phone_sales and PHONE_SALES.search(card + detail):
            fails.append("电话销售")
        if self.part_time and NOT_FULLTIME.search(card + detail):
            fails.append("兼职/实习")
        if (self.overseas or overseas) and not re.search(
                r"海外|跨境|出口|出海|驻外|国际|全球|境外|进出口", card + detail):
            fails.append("非海外/跨境")
        return (len(fails) == 0), fails


# ───────────────────────────── 驱动层 ─────────────────────────────

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


def get_cards() -> list:
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


GREETING_TEMPLATE = (
    (ROOT / "greeting.txt").read_text(encoding="utf-8").strip()
    if (ROOT / "greeting.txt").is_file()
    else "您好！我对贵司「{title}」岗位很感兴趣，希望能有机会进一步沟通，期待您的回复！"
)


def send_custom(title: str) -> tuple:
    """进聊天并发送按岗位定制的招呼语，发送后验证。"""
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
    return False, "发送未确认(消息未出现)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="商业分析")
    ap.add_argument("--city", default="101020100")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--min-gap", type=int, default=60)
    ap.add_argument("--max-gap", type=int, default=180)
    ap.add_argument("--max-refresh", type=int, default=6,
                    help="列表扫完后最多刷新几轮")
    ap.add_argument("--max-fail", type=int, default=3,
                    help="连续发送失败达到该次数即停整轮")
    ap.add_argument("--rules", default=str(RULES_FILE), help="规则文件路径")
    ap.add_argument("--overseas", action="store_true",
                    help="叠加要求海外/跨境信号")
    ns = ap.parse_args()

    rules = Rules(load_rules(Path(ns.rules)))
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

    sent, log, refresh_round, fail_streak = 0, [], 0, 0
    while sent < ns.limit and refresh_round < ns.max_refresh:
        cards = get_cards()
        print("第 %d 轮搜索，卡片数: %d" % (refresh_round + 1, len(cards)),
              flush=True)
        for c in cards:
            if sent >= ns.limit:
                break
            text = c["t"]
            first_line = text.splitlines()[0][:36] if text else ""
            key = first_line
            if key in greeted:
                continue
            why = rules.card_reject(text, first_line)
            if why:
                print("  跳过(%s): %s" % (why, first_line), flush=True)
                greeted.add(key)
                continue
            if click_card_by_text(first_line) != "clicked":
                continue
            time.sleep(4)
            if "security" in ev("location.href.slice(0,120)"):
                print("命中安全校验页，停止。已发 %d 条。" % sent, flush=True)
                break
            time.sleep(3)
            detail = call("text", {"sel": ".job-detail-box",
                                   "limit": 3000}, timeout=30).get("text", "")
            if not detail:
                greeted.add(key)
                continue
            why_d = rules.detail_reject(text, first_line, detail)
            if why_d:
                print("  跳过(%s): %s" % (why_d, first_line), flush=True)
                greeted.add(key)
                continue
            print("  详情核查通过: %s" % first_line, flush=True)
            ok_g, fails_g = rules.gate(text, first_line, detail, ns.overseas)
            if not ok_g:
                print("  校对未通过(放弃发送): %s | %s"
                      % (first_line, " / ".join(fails_g)), flush=True)
                greeted.add(key)
                continue
            if js_click("立即沟通") != "clicked":
                continue
            ok2, why2 = send_custom(first_line)
            print("  %s: %s (%s)" % ("OK" if ok2 else "FAIL",
                                    first_line, why2), flush=True)
            log.append("%s %s %s" % ("OK" if ok2 else "FAIL", first_line, why2))
            greeted.add(key)
            save_greeted(greeted)
            fail_streak = 0 if ok2 else fail_streak + 1
            if ok2:
                sent += 1
            if fail_streak >= ns.max_fail:
                print("连续 %d 次发送失败，疑似风控，停止本轮。已发 %d 条。"
                      % (fail_streak, sent), flush=True)
                break
            if sent < ns.limit:
                gap = random.randint(ns.min_gap, ns.max_gap)
                print("    休眠 %ds（随机间隔）..." % gap, flush=True)
                time.sleep(gap)
                r = call("navigate", {"url": url}, timeout=90)
                if "security" in str(r.get("url", "")):
                    print("疑似风控未解除，停止本轮。已发 %d 条。" % sent)
                    break
                time.sleep(10)
        if sent < ns.limit:
            refresh_round += 1
            wait = random.randint(25, 50)
            print("  本轮扫完，%ds 后刷新搜索（第 %d/%d 轮）..."
                  % (wait, refresh_round, ns.max_refresh), flush=True)
            time.sleep(wait)
            call("navigate", {"url": url}, timeout=90)
            time.sleep(12)
            if "security" in ev("location.href.slice(0,120)"):
                print("刷新后命中安全校验页，停止。已发 %d 条。" % sent)
                break

    print(NL + "==== 本轮发送 %d 条（刷新 %d 轮）====" % (sent, refresh_round))
    ROOT.joinpath("boss_batch_log.txt").write_text(NL.join(log), encoding="utf-8")


if __name__ == "__main__":
    main()
