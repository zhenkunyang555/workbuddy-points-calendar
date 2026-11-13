#!/usr/bin/env python3
"""今天每个账号花了多少积分：实时版（每 5 秒重算一次）。

数据源：/cfg/magpie/usage.jsonl —— magpie 每完成一次请求就追一行，
带 providerAccount（哪个账号）和 credits（这次扣了多少分）。
只统计 provider=workbuddy 的行（那才是 WorkBuddy 积分）。

和页面上那张柱状图的区别：柱状图用 magpie 的 credits-daily.json（按余额差记账，
要等它自己轮询余额才更新）；这里按每次请求累加，随请求立刻变。两边差 1% 上下属正常。

输出 /app/today.json，供 report.html 每 5 秒轮询。
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path("/cfg/magpie/usage.jsonl")
OUT = Path("/app/today.json")
REP = Path("/app/report.json")
LOG = Path("/app/guard.log")
CST = timezone(timedelta(hours=8))
TICK = 5
KEEP_DAYS = 7

S = {"offset": 0, "carry": b"", "days": {}, "last_req": None}


def log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(CST):%Y-%m-%d %H:%M:%S}  [today] {msg}\n")
    except Exception:
        pass


def local_day(t):
    """请求时间 → 本地（+08:00）日期。老版本 magpie 写的是 UTC 的 Z 结尾，直接切前 10 位会算错日子。"""
    s = str(t or "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00")) if s.endswith("Z") \
            else datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST).date().isoformat()
    except Exception:
        return s[:10] if len(s) >= 10 else ""


def scan():
    """从上次读到的位置往后读，把新出现的 workbuddy 请求累加进按天、按账号的账。"""
    try:
        st = SRC.stat()
    except FileNotFoundError:
        S["offset"], S["carry"] = 0, b""
        return 0
    if st.st_size < S["offset"]:          # 文件被截断或轮转：从新的文件头继续，已累计的数不清零
        S["offset"], S["carry"] = 0, b""
    if st.st_size == S["offset"]:
        return 0
    with SRC.open("rb") as f:
        f.seek(S["offset"])
        chunk = f.read()
    S["offset"] += len(chunk)
    buf = S["carry"] + chunk
    lines = buf.split(b"\n")
    S["carry"] = lines.pop()              # 末行可能还没写完，留到下一轮
    added = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("provider") != "workbuddy":
            continue
        day_key = local_day(d.get("t"))
        if len(day_key) != 10:
            continue
        who = d.get("providerAccount") or "?"
        try:
            c = float(d.get("credits") or 0)
        except Exception:
            c = 0.0
        bucket = S["days"].setdefault(day_key, {})
        bucket[who] = bucket.get(who, 0.0) + c
        ts = str(d.get("t") or "")[:19].replace("T", " ")
        if ts and (S["last_req"] is None or ts > S["last_req"]):
            S["last_req"] = ts
        added += 1
    if len(S["days"]) > KEEP_DAYS:        # 只留最近几天
        for k in sorted(S["days"])[:-KEEP_DAYS]:
            S["days"].pop(k, None)
    return added


def order():
    """账号顺序跟页面顶部的账号总览保持一致，新账号接在最后。"""
    names = []
    try:
        rep = json.loads(REP.read_text(encoding="utf-8"))
        for a in rep.get("accounts") or []:
            u = a.get("user")
            if u and u not in names:
                names.append(u)
    except Exception:
        pass
    return names


def write():
    now = datetime.now(CST)
    today = now.date().isoformat()
    yday = (now.date() - timedelta(days=1)).isoformat()
    td = S["days"].get(today, {})
    yd = S["days"].get(yday, {})
    names = order()
    for n in list(td) + list(yd):
        if n not in names:
            names.append(n)
    total_t = sum(td.values())
    total_y = sum(yd.values())
    accounts = []
    for n in names:
        t = round(td.get(n, 0.0), 2)
        accounts.append({
            "user": n,
            "today": t,
            "yesterday": round(yd.get(n, 0.0), 2),
            "share": round(t / total_t * 100, 1) if total_t > 0 else None,
        })
    out = {
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "yesterday": yday,
        "total_today": round(total_t, 2),
        "total_yesterday": round(total_y, 2),
        "last_req": S["last_req"],
        "source": "magpie usage.jsonl（按每次请求累加）",
        "accounts": accounts,
    }
    tmp = OUT.with_name("today.json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OUT)


def main():
    first = scan()
    log(f"启动：历史 workbuddy 请求 {first} 条，已读 {S['offset']} 字节")
    last_day = None
    while True:
        try:
            scan()
            write()
            day = datetime.now(CST).date().isoformat()
            if day != last_day:
                log(f"换日：{day}")
                last_day = day
        except Exception as e:
            log(f"出错：{e}")
        time.sleep(TICK)


if __name__ == "__main__":
    raise SystemExit(main())
