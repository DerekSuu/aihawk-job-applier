"""长驻浏览器守护进程。

它存在的理由只有一条：浏览器必须活着。

单步驱动的意思是导航、看、填、点分成一次次的独立调用，而每一次调用之间
浏览器如果关了，登录态还没事（走持久化 profile），但填到一半的表单没了。
所以这里开一个进程把 Playwright 的持久化上下文一直抱在手里，对外只暴露
一个 localhost 的 HTTP 接口，命令行客户端随用随走。

线程模型（这一条是被实际故障逼出来的，不是设计洁癖）：
Playwright 的同步 API 绑定在创建它的那个线程上——从别的线程调它，报
"Cannot switch to a different thread"。而 HTTP server 每个请求一个线程。
所以所有浏览器动作必须投送到唯一的工作线程执行。这个约束顺带解决了另一
件本来要单独处理的事：两条命令同时操作一个浏览器，等于两只手抢一个鼠标。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .condense import condense, render

# 在页面里执行的抽取脚本。
#
# 刻意在浏览器进程内把 DOM 走一遍、只回传结论，而不是把 innerHTML 搬回来
# 再在 Python 里解析：一页 40K 字符的 HTML 传回来就是 40K 字符的流量，
# 抽取之后通常只剩 1-3K，省掉的是同一个数量级的浪费。
JS_EXTRACT = r"""
() => {
  const out = {labels: {}, elements: []};
  document.querySelectorAll('label[for]').forEach(l => {
    const t = (l.innerText || '').trim();
    if (t) out.labels[l.getAttribute('for')] = t.replace(/[:：*\s]+$/, '');
  });

  const path = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const n = el.getAttribute('name');
    if (n) return el.tagName.toLowerCase() + '[name="' + n.replace(/"/g, '\\"') + '"]';
    const parts = [];
    let cur = el, depth = 0;
    while (cur && cur.nodeType === 1 && depth < 4) {
      let s = cur.tagName.toLowerCase();
      const p = cur.parentElement;
      if (p) {
        const sibs = Array.from(p.children).filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      }
      parts.unshift(s);
      cur = cur.parentElement; depth++;
    }
    return parts.join(' > ');
  };

  const attr = (el, k) => { try { return el.getAttribute(k) || ''; } catch (e) { return ''; } };

  document.querySelectorAll('input,select,textarea,button,a').forEach(el => {
    const tag = el.tagName.toLowerCase();
    const type = (attr(el, 'type') || '').toLowerCase();
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if ((r.width <= 0 || r.height <= 0) &&
        (style.display === 'none' || style.visibility === 'hidden')) return;

    const rec = {
      tag: tag,
      type: type,
      id: el.id || '',
      name: attr(el, 'name'),
      selector: path(el),
      required: el.required === true || attr(el, 'aria-required') === 'true',
      placeholder: attr(el, 'placeholder'),
      ariaLabel: attr(el, 'aria-label'),
      text: (el.innerText || el.value || '').slice(0, 60),
    };
    if (tag === 'select') {
      rec.options = Array.from(el.options).map(o => o.text.trim());
      rec.value = el.value;
    } else if (type === 'checkbox' || type === 'radio') {
      rec.checked = el.checked;
      rec.value = el.value;
    } else if (tag !== 'button' && tag !== 'a') {
      rec.value = (el.value || '').slice(0, 120);
    }
    out.elements.push(rec);
  });

  return out;
}
"""

# 登录页特征。命中即提示"可能不在已登录状态"。
LOGIN_MARKERS = (
    "login", "signin", "sign-in", "passport", "sso", "cas.", "auth",
    "登录", "登陆", "注册", "验证码",
)

STOP = object()


class Browser:
    """所有 Playwright 调用的唯一入口，且只允许在工作线程上被使用。"""

    def __init__(self, profile: Path, headless: bool, slow_mo: int = 0,
                 cdp_endpoint: str | None = None,
                 executable_path: str | None = None) -> None:
        self.profile = profile
        self.headless = headless
        self.slow_mo = slow_mo
        # 非 None 表示接管一个已在运行的浏览器（用户的 Chrome），而不是自己起一个。
        # 此时登录态由那个浏览器自身的 profile 负责，与 self.profile 无关，
        # 也就不需要再做登录态预热。
        self.cdp_endpoint = cdp_endpoint
        # stealth 反检测引擎（invisible-playwright 的补丁版 firefox.exe）。
        # 普通 Playwright Firefox 的指纹会被强风控站点（BOSS 直聘）识别，
        # 而 stealth 引擎能绕过。把它当 executable_path 传给标准 playwright
        # firefox，即可保留本 daemon 全部能力 + stealth 指纹。
        self.executable_path = executable_path
        self._pw = None
        self._ctx = None
        self._cdp_browser = None
        self.page = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        if self.cdp_endpoint:
            self._start_cdp()
            return

        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            self._ctx = self._pw.firefox.launch_persistent_context(
                user_data_dir=str(self.profile),
                headless=self.headless,
                slow_mo=self.slow_mo,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                executable_path=self.executable_path,
            )
        except Exception as exc:
            raise RuntimeError("%s\n\n%s" % (exc, self._lock_hint())) from exc
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _start_cdp(self) -> None:
        """接管用户自己启动的浏览器（需带 --remote-debugging-port）。

        关键差异有三条，都源自"这个浏览器不属于我们"：
        1. 不能设 viewport / locale / timezone——进程已经在跑，改不了。
        2. close() 时绝不能关它，否则等于把用户的浏览器连同标签页一起杀掉。
        3. 页面可能一个都没有（刚启动只开了空白页），也可能有很多个；
           取第一个已有的，没有就新建一个，绝不碰其余标签页。
        """
        self._cdp_browser = self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
        ctxs = self._cdp_browser.contexts
        self._ctx = ctxs[0] if ctxs else None
        if self._ctx is None:
            raise RuntimeError(
                "连上了 %s 但拿不到任何浏览器上下文。通常是浏览器刚启动还没有窗口，"
                "请先手动打开一个窗口再重试。" % self.cdp_endpoint)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def pages(self) -> dict:
        """列出当前上下文的全部标签页。

        背景：Apply 类按钮经常弹新窗口（如宝洁的 Moka 投递层），而 daemon
        只跟踪第一个 page。没有这条命令时，弹窗对我们是不可见的——人看得见，
        程序够不着。列出来，再用 use 切过去。
        """
        return {
            "pages": [
                {"index": i, "url": p.url[:100], "title": (p.title() or "")[:60]}
                for i, p in enumerate(self._ctx.pages)
            ]
        }

    def use(self, index: int) -> dict:
        """把后续命令的目标切到第 index 个标签页（不关闭、不动其他页）。"""
        if not (0 <= index < len(self._ctx.pages)):
            return {"error": "index 越界：0-%d" % (len(self._ctx.pages) - 1)}
        self.page = self._ctx.pages[index]
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        return {"ok": True, "index": index, "url": self.page.url[:100]}

    def _fillable(self):
        """当前页所有可见的 input/textarea（排除 checkbox/radio/file/隐藏）。"""
        return [
            el for el in self.page.query_selector_all("input,textarea")
            if el.evaluate("""el => {
                if (el.offsetParent === null && el.type !== 'hidden') return false;
                return !['checkbox','radio','file','submit','button'].includes(el.type);
            }""")
        ]

    def inputs(self, limit: int = 120) -> dict:
        """列出全部可填字段及序号——快照选择器重名时，按序号填是唯一可靠路径。"""
        out = []
        for i, el in enumerate(self._fillable()):
            info = el.evaluate("""el => ({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                ph: (el.placeholder || '').slice(0, 24),
                label: (el.closest('label') ? el.closest('label').innerText :
                        (el.closest('div') ? el.closest('div').innerText : '') || '')
                      .replace(/\\s+/g, ' ').slice(0, 30),
                value: (el.value || '').slice(0, 40),
            })""")
            out.append({"n": i, **info})
        return {"fields": out[:limit]}

    def filln(self, n: int, value: str) -> dict:
        """按序号填写第 n 个可填字段（见 inputs 的编号）。"""
        els = self._fillable()
        if not (0 <= n < len(els)):
            return {"error": "n 越界：0-%d" % (len(els) - 1)}
        els[n].fill(value)
        return {"ok": True, "n": n, "value": value[:40]}

    def _lock_hint(self) -> str:
        """启动失败时，先怀疑 profile 被上一个没退干净的进程占着。

        这是本项目里必然反复发生的事：会话一关、daemon 被杀，Firefox 来不及
        清掉 parent.lock，下次启动直接失败，而报错只说"browser process
        exited"，看不出跟锁有关。与其让人每次都重新查一遍，不如在这里直说。
        """
        locks = [p for p in ("parent.lock", ".parentlock")
                 if (self.profile / p).exists()]
        if not locks:
            return "未发现 profile 锁文件。"
        return (
            "最可能的原因：profile 目录被上一个未正常退出的浏览器占着（发现 %s）。\n"
            "  1. 确认没有别的 daemon 在跑：tasklist | findstr firefox\n"
            "  2. 确认后清除锁：rm -f %s\n"
            "  3. 重启 daemon\n"
            "注意：只有在确认没有进程占用时才删锁，删错了会损坏 profile 里的登录态。"
            % ("/".join(locks), " ".join(str(self.profile / p) for p in locks))
        )

    def close(self) -> None:
        # 接管模式下只断开连接，绝不关闭浏览器本身——那是用户的浏览器。
        if self._cdp_browser is not None:
            try:
                self._cdp_browser.close()   # 对 CDP 连接而言这是 disconnect
            except Exception:
                pass
            self._cdp_browser = None
            self._ctx = None
            self.page = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            return

        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass

    # ---- 动作 -----------------------------------------------------------

    def navigate(self, url: str, wait: str = "domcontentloaded", timeout: int = 30000) -> dict:
        if not re.match(r"^https?://", url):
            return {"error": "只接受 http/https：%s" % url}
        self.page.goto(url, wait_until=wait, timeout=timeout)
        return {"url": self.page.url, "title": self.page.title()}

    def snapshot(self, want_links: bool = False) -> dict:
        raw = self.page.evaluate(JS_EXTRACT)
        raw["url"] = self.page.url
        raw["title"] = self.page.title()
        raw["want_links"] = want_links
        raw["login_state"] = self._guess_login()
        report = condense(raw)
        return {"report": report, "text": render(report)}

    def links(self, limit: int = 40) -> dict:
        raw = self.page.evaluate(JS_EXTRACT)
        out = []
        for el in raw.get("elements", []):
            if el.get("tag") == "a" and (el.get("text") or "").strip():
                out.append({"sel": el["selector"], "text": el["text"].strip()[:60]})
        return {"links": out[:limit]}

    def fill(self, sel: str, value: str, timeout: int = 8000) -> dict:
        self.page.wait_for_selector(sel, timeout=timeout)
        self.page.fill(sel, value)
        return {"ok": True, "sel": sel, "len": len(value)}

    def click(self, sel: str, timeout: int = 8000) -> dict:
        self.page.wait_for_selector(sel, timeout=timeout)
        self.page.click(sel, timeout=timeout)
        time.sleep(0.4)
        return {"ok": True, "url": self.page.url}

    def select(self, sel: str, value: str, timeout: int = 8000) -> dict:
        self.page.wait_for_selector(sel, timeout=timeout)
        self.page.select_option(sel, value, timeout=timeout)
        return {"ok": True, "sel": sel, "value": value}

    def upload(self, sel: str, path: str, timeout: int = 8000) -> dict:
        p = Path(path)
        if not p.is_file():
            return {"error": "文件不存在：%s" % path}
        # 关键：不能等 visible。Moka 的 <input type=file> 是刻意隐藏的
        # （#resumeKey offsetParent=null），真正的上传按钮才是可见的。
        # set_input_files 直接作用于 input 元素，本就不需要可见性；
        # 这里只等它"挂载"（state=attached），等 visible 会在真站点上必超时。
        self.page.wait_for_selector(sel, state="attached", timeout=timeout)
        self.page.set_input_files(sel, str(p))
        return {"ok": True, "file": p.name}

    def text(self, sel: str = "body", limit: int = 4000) -> dict:
        self.page.wait_for_selector(sel, timeout=8000)
        t = self.page.inner_text(sel)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        return {"text": t[:limit], "truncated": len(t) > limit}

    def screenshot(self, out: str, full: bool = False) -> dict:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(p), full_page=full)
        return {"ok": True, "path": str(p)}

    def evaluate(self, expression: str, limit: int = 3000) -> dict:
        v = self.page.evaluate(expression)
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return {"result": s[:limit]}

    def sleep(self, seconds: float) -> dict:
        time.sleep(min(max(seconds, 0), 600))
        return {"ok": True, "slept": seconds}

    def _guess_login(self) -> str:
        """尽力判断当前是否处于登录态。

        这是启发式，不是保证：把"URL 里出现 login"当成未登录，在很多站点
        是对的，但有些站点登录成功后的落地页本身就带 login 字样。所以结论
        一律回传给上层，由调用方（我）结合页面内容复核，不在这里替它做决定。
        """
        u = (self.page.url or "").lower()
        for m in LOGIN_MARKERS:
            if m in u:
                return "可能未登录（URL 命中 '%s'）" % m
        try:
            body = (self.page.inner_text("body") or "")[:1500]
        except Exception:
            return "无法判断"
        hits = sum(1 for m in ("登录", "登陆", "验证码", "立即注册") if m in body)
        if hits >= 2:
            return "可能未登录（页面文字）"
        if hits == 0:
            return "可能已登录"
        return "不确定"

    def status(self) -> dict:
        return {"url": self.page.url if self.page else None}


class Worker(threading.Thread):
    """独占浏览器、串行消费命令队列的那一个线程。"""

    def __init__(self, browser: Browser) -> None:
        super().__init__(daemon=True)
        self.browser = browser
        self.q: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.pause_reason = ""

    def run(self) -> None:
        try:
            self.browser.start()
        except Exception as exc:
            self.start_error = "%s: %s" % (type(exc).__name__, exc)
            self.ready.set()
            return
        self.ready.set()
        while True:
            item = self.q.get()
            if item is STOP:
                break
            cmd, args, box = item
            try:
                box["out"] = self._dispatch(cmd, args)
            except Exception as exc:
                box["out"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            box["event"].set()

    def _dispatch(self, cmd: str, args: dict) -> dict:
        b = self.browser
        table = {
            "navigate": b.navigate, "snapshot": b.snapshot, "links": b.links,
            "fill": b.fill, "click": b.click, "select": b.select,
            "upload": b.upload, "text": b.text, "screenshot": b.screenshot,
            "eval": b.evaluate, "wait": b.sleep,
            "pages": b.pages, "use": b.use,
            "inputs": b.inputs, "filln": b.filln,
        }
        if cmd not in table:
            return {"error": "未知命令：%s" % cmd}
        return table[cmd](**args)

    def submit(self, cmd: str, args: dict, timeout: float) -> dict:
        """从任意线程投一条命令，阻塞等结果。"""
        if not self.gate.wait(timeout=timeout):
            return {"error": "已暂停，等待 resume（原因：%s）" % self.pause_reason}
        box: dict[str, Any] = {"event": threading.Event(), "out": None}
        self.q.put((cmd, args, box))
        if not box["event"].wait(timeout=timeout):
            return {"error": "命令超时（%ss）：%s" % (timeout, cmd)}
        return box["out"]


class Handler(BaseHTTPRequestHandler):
    worker: Worker
    started: float

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send({"error": "仅允许本机访问"}, 403)
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:
            self._send({"error": "请求格式错误：%s" % exc}, 400)
            return

        cmd = payload.get("cmd")
        args = payload.get("args") or {}

        if cmd == "shutdown":
            self.worker.q.put(STOP)
            self._send({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if cmd == "resume":
            self.worker.pause_reason = ""
            self.worker.gate.set()
            self._send({"ok": True, "resumed": True})
            return

        if cmd == "pause":
            # 把"该你输验证码了"这个时刻真正钉住：不 resume，后面的命令进不来。
            self.worker.pause_reason = args.get("message", "")
            self.worker.gate.clear()
            self._send({"ok": True, "paused": True, "message": self.worker.pause_reason})
            return

        out = self.worker.submit(cmd, args, timeout=float(args.pop("_timeout", 120)))
        self._send(out if isinstance(out, dict) else {"result": out})

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send({
                "ok": True,
                "uptime_s": round(time.time() - self.started),
                "paused": not self.worker.gate.is_set(),
                "pause_reason": self.worker.pause_reason,
                "queued": self.worker.q.qsize(),
                **self.worker.browser.status(),
            })
            return
        self._send({"error": "not found"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser(description="长驻浏览器守护进程")
    ap.add_argument("--profile", required=True, help="持久化 profile 目录（登录态存在这里）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（人工介入时必需）")
    ap.add_argument("--port", type=int, default=0, help="0 = 自动选端口并写入 state 文件")
    ap.add_argument("--state", default=None, help="端口/健康信息写入位置")
    ap.add_argument("--slow-mo", type=int, default=0, help="每步动作之间插入的毫秒数，模拟人手速")
    ap.add_argument("--cdp", default=None, metavar="URL",
                    help="接管已运行的浏览器，如 http://127.0.0.1:9222。"
                         "需先用 --remote-debugging-port 启动 Chrome。"
                         "此时 --profile 仅作占位，登录态用浏览器自己的。")
    ap.add_argument("--stealth-binary", default=None, metavar="PATH",
                    help="用 stealth 反检测 firefox.exe（invisible-playwright 补丁版）。"
                         "强风控站点如 BOSS 直聘会识别普通 Playwright Firefox 指纹并拦截，"
                         "stealth 引擎可绕过。指定后本 daemon 用标准 playwright 驱动该引擎。")
    args = ap.parse_args()

    worker = Worker(Browser(Path(args.profile), headless=not args.headed,
                            slow_mo=args.slow_mo, cdp_endpoint=args.cdp,
                            executable_path=args.stealth_binary))
    worker.start()
    if not worker.ready.wait(timeout=90):
        raise SystemExit("浏览器启动超时")
    if getattr(worker, "start_error", None):
        raise SystemExit("浏览器启动失败：%s" % worker.start_error)

    Handler.worker = worker
    Handler.started = time.time()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = httpd.server_address[1]

    state = Path(args.state) if args.state else Path(args.profile) / "daemon.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "port": port,
        "profile": str(Path(args.profile).resolve()),
        "headed": args.headed,
        "pid": os.getpid(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print("daemon  port=%d  profile=%s  headed=%s" % (port, args.profile, args.headed))
    print("state   %s" % state)
    try:
        httpd.serve_forever()
    finally:
        state.unlink(missing_ok=True)
        worker.q.put(STOP)
        worker.browser.close()


if __name__ == "__main__":
    main()
