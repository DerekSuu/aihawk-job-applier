"""单步驱动的命令行客户端。

每次调用是一个独立进程：发一条命令、拿结果、退出。浏览器不在它手里，
在 daemon 里。这个拆分是刻意的——进程挂了浏览器不挂，填到一半的表单还在。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_STATE = Path(__file__).resolve().parents[2] / "profiles" / "default" / "daemon.json"


def _port(state: Path) -> int:
    if not state.is_file():
        sys.exit("daemon 未运行（找不到 %s）。先跑：\n"
                 "  .venv/Scripts/python.exe -m jobdriver.daemon --profile profiles/default --headed"
                 % state)
    return json.loads(state.read_text(encoding="utf-8"))["port"]


def _opener() -> urllib.request.OpenerDirector:
    """显式不走任何代理。

    不是绕开什么，而是修正一个真实的错：环境里一旦设了 http_proxy，
    urllib 会把 127.0.0.1 的请求也送进代理，代理转头返回 502
    "upstream connect failed"，看起来像 daemon 挂了，其实它好好的。
    回环地址从来没有理由经过代理。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(port: int, cmd: str, args: dict, timeout: float) -> dict:
    body = json.dumps({"cmd": cmd, "args": args}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/cmd" % port, data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with _opener().open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except TimeoutError:
        # 暂停期间被闸门挡住时会走到这里。这是预期结果，不是故障，
        # 说清楚"被挡住了、为什么"比抛一段栈回溯有用得多。
        return {"error": "等待 %ss 无响应：daemon 可能正处于暂停状态，"
                         "先执行 resume 再重试；也可能是页面加载卡住。" % timeout}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 代理或中间件插进来时会走到这里。给出可诊断的原文而不是崩栈。
        return {"error": "non-json response (%d chars): %s" % (len(raw), raw[:200])}


def _emit(out: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    if "error" in out:
        print("ERROR  %s" % out["error"], file=sys.stderr)
        sys.exit(1)
    if "text" in out and isinstance(out["text"], str) and len(out) <= 2:
        print(out["text"])
        return
    print(json.dumps(out, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="drive", description="单步驱动浏览器")
    ap.add_argument("cmd")
    ap.add_argument("rest", nargs="*", help="key=value，值含空格时加引号")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()

    args: dict = {}
    for item in ns.rest:
        if "=" not in item:
            sys.exit("参数必须写成 key=value：%r" % item)
        k, v = item.split("=", 1)
        if v.lower() in ("true", "false"):
            args[k] = (v.lower() == "true")
        elif v.isdigit():
            args[k] = int(v)
        else:
            args[k] = v

    _emit(_call(_port(Path(ns.state)), ns.cmd, args, ns.timeout), ns.json)


if __name__ == "__main__":
    main()
