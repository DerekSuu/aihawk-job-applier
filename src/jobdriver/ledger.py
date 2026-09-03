"""最小去重集合。

刻意不做台账：状态、结果、时间戳一概不记，因为每一步都有人在场，
人自己知道投过什么。这里只回答一个问题——"这家投过没有"。

重复投递不是效率问题，是印象问题：同一家公司收到两份一模一样的简历，
招聘方的判断不会是"这人很积极"。所以这条底线必须留着，哪怕用户说不用记录。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
    key      TEXT PRIMARY KEY,   -- 公司+岗位的归一化哈希
    company  TEXT NOT NULL,
    role     TEXT,
    channel  TEXT,               -- 官网 / BOSS / 猎聘
    url      TEXT,
    resume   TEXT,               -- 用的是哪份方向简历
    sent_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company ON sent(company);
"""


def normalize(text: str) -> str:
    """把公司名和岗位名压成可比较的形式。

    同一个岗位在不同页面会写成"商业分析岗"、"商业分析（校招）"、
    "Commercial Analyst"，全角半角、空格、括号都不一致。
    不去这些噪音，去重就形同虚设。
    """
    t = unicodedata.normalize("NFKC", text or "").strip().lower()
    t = re.sub(r"[\s　]+", "", t)
    t = re.sub(r"[（(].*?[)）]", "", t)          # 去掉括号补充说明
    t = re.sub(r"(校招|社招|应届生|正式批|提前批|管培生?|岗|职位)$", "", t)
    t = re.sub(r"[·•\-—_/、,，。.]", "", t)
    return t


def make_key(company: str, role: str = "") -> str:
    raw = normalize(company) + "|" + normalize(role)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Ledger:
    def __init__(self, path: str | Path = "sent.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.executescript(SCHEMA)
        self._db.commit()

    def seen(self, company: str, role: str = "") -> bool:
        row = self._db.execute(
            "SELECT 1 FROM sent WHERE key=?", (make_key(company, role),)
        ).fetchone()
        return row is not None

    def seen_company(self, company: str) -> bool:
        """只按公司查。

        同一家公司投两个岗，在招聘方系统里常常是同一个人的两条记录，
        大部分情况下仍然应当避开。需要放宽时由调用方显式决定。
        """
        row = self._db.execute(
            "SELECT 1 FROM sent WHERE company=? LIMIT 1", (normalize(company),)
        ).fetchone()
        return row is not None

    def record(self, company: str, role: str = "", channel: str = "",
               url: str = "", resume: str = "") -> bool:
        """记一条。已存在则不覆盖，返回是否新写入。"""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO sent VALUES (?,?,?,?,?,?,?)",
            (make_key(company, role), normalize(company), role,
             channel, url, resume, datetime.now().isoformat(timespec="seconds")),
        )
        self._db.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM sent").fetchone()[0]

    def recent(self, limit: int = 10) -> list[dict]:
        rows = self._db.execute(
            "SELECT company, role, channel, resume, sent_at FROM sent "
            "ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(zip(("company", "role", "channel", "resume", "sent_at"), r))
                for r in rows]

    def close(self) -> None:
        self._db.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="投递去重集合")
    ap.add_argument("cmd", choices=["check", "add", "list", "count"])
    ap.add_argument("company", nargs="?")
    ap.add_argument("--role", default="")
    ap.add_argument("--channel", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--resume", default="")
    ap.add_argument("--db", default="sent.db")
    ns = ap.parse_args()

    led = Ledger(ns.db)
    if ns.cmd == "check":
        if not ns.company:
            ap.error("check 需要给公司名")
        print("已投" if led.seen(ns.company, ns.role) else "未投")
    elif ns.cmd == "add":
        print("新记录" if led.record(ns.company or "", ns.role, ns.channel,
                                     ns.url, ns.resume) else "重复，跳过")
    elif ns.cmd == "count":
        print(led.count())
    else:
        for r in led.recent():
            print("%(sent_at)s  %(company)s  %(role)s  [%(channel)s]" % r)
    led.close()
