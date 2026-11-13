#!/usr/bin/env python3
"""账号策略 v6：一个号都不关，只挪「登录位」。

两条实测结论支撑这个版本：

1. **关号省不下任何积分。** 签到、任务、派猫、抽奖都由 tasks.py / claim.py / extra.py 拿
   每个号自己的令牌直接调腾讯接口，跟 magpie 的 on/off 毫无关系。2026-11-11 实测：
   某个号在 magpie 里是 off，checkin-activity-status 照样返回
   active=True / today_checked_in=True / today_credit=100。所以 v5「关掉长期号、少进点
   将来会作废的分」是错的——一分没省，还白丢一个能烧分的席位，并逼着脚本专门开窗口续令牌。

2. **当前登录的那个号不进排名**（实测 rank=None），4 个号全开时永远有 1 个在吃闲饭。
   把这条冷板凳安排给「30 天内到期最少」的号，就等于把损失压到最小。

v6 的排法：
  · 全部账号常开，签到/任务/抽奖一个不落（不关任何一个）；
  · 登录位（不排名的那一位）在「到期最少的几个号」之间每 12 小时轮换一次；
  · **30 天内到期最多的那个号永远不坐登录位**，保证它一直占一个真排名席位烧分；
  · 谁到期最多是按 report.json 实时算的，大户烧干了自动让位给下一位。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "http://192.168.172.13:3430"
DIR = Path("/app")
KEYF = DIR / "web.key"
OUT = DIR / "budget.json"
STATE = DIR / "budget-state.json"
VALVE_STATE = DIR / "valve-state.json"
AGENT = "workbuddy"
SEAT_EVERY = 12 * 3600     # 登录位轮换周期
CST = timezone(timedelta(hours=8))


def log(s):
    with (DIR / "guard.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [policy] {s}\n")


def opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(op, path):
    return json.loads(op.open(f"{BASE}/api/{path}", timeout=25).read())


def post(op, path, body):
    req = urllib.request.Request(f"{BASE}/api/{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with op.open(req, timeout=25) as r:
            t = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(t) if t.strip().startswith("{") else {})
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(t)
        except Exception:
            return e.code, {"raw": t[:150]}
    except Exception as e:
        return 0, {"error": str(e)}


def report_accounts():
    """{账号: {total, near30, next_end, next_left}}，取自 report.json（最近一次抓取）。"""
    try:
        rep = json.loads((DIR / "report.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for a in rep.get("accounts") or []:
        u = a.get("user")
        if not u:
            continue
        out[u] = {
            "total": float(a.get("total_left") or 0),
            "near30": float(a.get("near30") or 0),
            "next_end": a.get("earliest") or "",
            "next_left": float(a.get("earliest_amount") or 0),
        }
    return out


def main():
    try:
        key = KEYF.read_text(encoding="utf-8").strip()
    except Exception:
        log("没有 web.key，跳过")
        return 1
    op = opener()
    try:
        op.open(f"{BASE}/api/groups?k={urllib.parse.quote(key)}", timeout=15).read()
    except Exception as e:
        log(f"登录 magpie 失败：{e}")
        return 1

    try:
        ps = get(op, "providers")
        ps = ps.get("providers") or ps
        w = next(x for x in ps if x.get("id") == AGENT)
        logins = (w.get("account") or {}).get("logins") or []
    except Exception as e:
        log(f"读 providers 失败：{e}")
        return 1
    known = [l.get("user") for l in logins if l.get("user")]
    if not known:
        log("没有登录号，跳过")
        return 1
    on_map = {l.get("user"): bool(l.get("on")) for l in logins}
    cur = next((l.get("user") for l in logins if l.get("active")), None)

    changes = []

    # 1 · 一个都不许关：凡是 off 的一律打开
    for u in known:
        if not on_map.get(u):
            st, b = post(op, "login/on", {"Agent": AGENT, "User": u})
            if 200 <= st < 300:
                on_map[u] = True
                changes.append(f"{u} 打开（关着不赚也不烧，纯亏）")
                log(changes[-1])
            else:
                log(f"{u} 打开失败：{st} {str(b)[:80]}")

    # 2 · 登录位：到期最少的几个号每 12 小时轮一次，到期最多的号永远不坐
    info = report_accounts()
    ranked = sorted([u for u in known if u in info], key=lambda u: -info[u]["near30"])
    big = ranked[0] if ranked else None
    seat_pool = list(reversed(ranked[1:]))          # 最少的排前面，先坐
    now = time.time()
    if seat_pool:
        want = seat_pool[int(now // SEAT_EVERY) % len(seat_pool)]
    else:
        want = big or cur
    if cur != want:
        st, b = post(op, "login/switch", {"Agent": AGENT, "User": want})
        if 200 <= st < 300:
            changes.append(f"登录位轮到 {want}（不参与排名；{big} 到期最多，不在板凳上）")
            log(changes[-1])
        else:
            log(f"登录位切不过去（{st} {str(b)[:80]}），本轮维持 {cur}")
            want = cur or want

    # 3 · 写 budget.json 给页面
    accts = []
    for u in known:
        d = info.get(u, {})
        accts.append({
            "user": u,
            "total": d.get("total", 0),
            "near30": d.get("near30", 0),
            "next_end": d.get("next_end", ""),
            "next_left": d.get("next_left", 0),
            "on": on_map.get(u, False),
            "role": ("登录位（不排名·暂不烧）" if u == want else "排名里·全速烧"),
        })
    accts.sort(key=lambda a: -a["near30"])
    order = [a["user"] for a in accts]
    OUT.write_text(json.dumps({
        "generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "全员常开 · 登录位轮换",
        "target": want,
        "backup": big or "",
        "rotation": order,
        "position": order.index(want) if want in order else 0,
        "rotate_below": 0,
        "burn_per_day": 0,
        "enabled": sorted(u for u in known if on_map.get(u)),
        "accounts": accts,
        "changes": changes,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    STATE.write_text(json.dumps({"seat": want, "seat_at": now}, ensure_ascii=False),
                     encoding="utf-8")
    log(f"登录位 {want}；全开 {len([1 for u in known if on_map.get(u)])} 个"
        + ("" if changes else "；无需调整"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
