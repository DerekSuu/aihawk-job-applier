"""进程结束后的产物清理：删除截图/状态/日志/锁，保留登录态与个人数据。

用法（daemon 关闭后执行）：
  .venv/Scripts/python.exe clean.py            # 预览将删除什么
  .venv/Scripts/python.exe clean.py --go       # 实际删除

绝不触碰：profiles/*/cookies.sqlite（登录态）、profile.yaml、resume/、
greeting.txt、greeted.json（去重）、targets.yaml、src/、.venv。
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 可安全删除的通配（相对 ROOT）
DELETE_PATTERNS = [
    "boss_*.png",          # 运行截图（含页面个人信息）
    "byte_form.png",
    "*.tmp",
    "profiles/parent.lock",   # 守护进程遗留锁
    "profiles/stealth.json",  # 端口状态（daemon 启动时重建）
    "profiles/default.json",
    "profiles/smoke.log",
    "fields*.json",        # 调试转储
    "recon*.json",         # 探测结果
    "boss_pay_page.html",  # 页面转储
    "boss_batch_log.txt",  # 投递历史
]

# 锁文件：仅当没有 firefox 进程持有它时才删
LOCK_FILES = ["profiles/stealth/parent.lock", "profiles/stealth/.parentlock",
              "profiles/default/parent.lock"]


def collect() -> list[Path]:
    files: list[Path] = []
    for pat in DELETE_PATTERNS:
        files.extend(ROOT.glob(pat))
    import subprocess
    firefox_running = b"firefox.exe" in subprocess.run(
        ["tasklist"], capture_output=True).stdout.lower()
    if not firefox_running:
        for lk in LOCK_FILES:
            p = ROOT / lk
            if p.exists():
                files.append(p)
    return sorted(set(files))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="实际删除（默认仅预览）")
    ns = ap.parse_args()
    files = collect()
    total = sum(f.stat().st_size for f in files if f.is_file())
    for f in files:
        print(("DEL " if ns.go else "WILL DEL ") + f.name)
    print("\n共 %d 项，%.1f MB" % (len(files), total / 1048576))
    if ns.go and files:
        n = 0
        for f in files:
            try:
                f.unlink()
                n += 1
            except OSError as e:
                print("跳过 %s: %s" % (f.name, e))
        print("已删除 %d/%d 项。" % (n, len(files)))
    elif not ns.go:
        print("（预览模式，加 --go 执行删除）")


if __name__ == "__main__":
    main()
