#!/usr/bin/env python3
"""安全阀：只在「排名里的 WB 号全部排不上号（全忙）」时，临时拉起一个关着的号兜底。

2026-11-11 起职责变了：**到期抢救（开储备号让它烧近期分）交给 policy.py 的轮值主烧**，
这里不再按「30 天内到期 ≥200 分」开关账号——那套做法在实测里是空转的：
v4 把一个储备号开了三天，magpie 一次都没把请求发到它身上（它排第 3，而且排名不看到期日）。

现在只剩一个作用：所有能烧的号都在限流（rank 全空）时，临时开一个余额最大的关着的号，
最多 4 小时，任何一个号恢复排名就关回去。开着的号写进 valve-state.json 的 forced_until，
policy.py 会跳过这些号，不跟它抢开关。
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
STATE = DIR / "valve-state.json"
BUDGET = DIR / "budget.json"
CST = timezone(timedelta(hours=8))
AGENT = "workbuddy"
MAX_OPEN = 4 * 3600


def log(s):
    with (DIR / "guard.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [valve] {s}\n")


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


def balances():
    """{账号: 余额}，来自 report.json。"""
    try:
        rep = json.loads((DIR / "report.json").read_text(encoding="utf-8"))
        return {a["user"]: float(a.get("total_left") or 0)
                for a in rep.get("accounts") or [] if a.get("user")}
    except Exception:
        return {}


def policy_wants_on():
    try:
        b = json.loads(BUDGET.read_text(encoding="utf-8"))
        return set(b.get("enabled") or [])
    except Exception:
        return set()


def main():
    try:
        key = KEYF.read_text(encoding="utf-8").strip()
    except Exception:
        log("没有 web.key")
        return 1
    op = opener()
    try:
        op.open(f"{BASE}/api/groups?k={urllib.parse.quote(key)}", timeout=15).read()
    except Exception as e:
        log(f"登录失败：{e}")
        return 1

    st = {}
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        pass
    forced = {u: t for u, t in (st.get("forced_until") or {}).items() if t}

    # 最近一次路由里，WB 有几位入榜（rank 非空）
    resting_all = False
    others_back = False
    seen = "没有路由记录"
    try:
        ids = get(op, "gateway/trace").get("ids") or []
        if ids:
            r = get(op, f"gateway/route?id={ids[-1]}")
            wb = [e for e in (r.get("order") or []) if e.get("provider") == AGENT]
            ranked = [e for e in wb if e.get("rank") is not None]
            seen = f"WB 候选 {len(wb)} 个，入榜 {len(ranked)} 个"
            if wb and not ranked:
                resting_all = True
            others_back = any(e.get("who") not in forced for e in ranked)
    except Exception as e:
        log(f"读路由记录失败：{e}")

    logins = []
    try:
        ps = get(op, "providers")
        ps = ps.get("providers") or ps
        w = next(x for x in ps if x.get("id") == AGENT)
        logins = (w.get("account") or {}).get("logins") or []
    except Exception:
        pass
    on_map = {l.get("user"): bool(l.get("on")) for l in logins}
    active = next((l.get("user") for l in logins if l.get("active")), None)
    bal = balances()
    want = policy_wants_on()

    now = time.time()
    opened = None
    closed = []

    # 1) 全忙 → 拉一个关着的、余额最大的号
    if resting_all:
        cand = [u for u in bal if not on_map.get(u)]
        cand.sort(key=lambda u: -bal[u])
        if cand:
            u = cand[0]
            s, b = post(op, "login/on", {"Agent": AGENT, "User": u})
            if 200 <= s < 300:
                forced[u] = now + MAX_OPEN
                opened = u
                log(f"全部在用账号限流，临时开启 {u}（余额 {bal[u]:.0f}）兜底")

    # 2) 有人恢复了 / 超时 → 关回自己开的号（不动 policy 想开的号）
    for u, t in list(forced.items()):
        if u == opened:
            continue
        expired = now >= t
        if (others_back or expired) and u not in want:
            s, b = post(op, "login/off", {"Agent": AGENT, "User": u})
            if 200 <= s < 300 or s == 0:
                forced.pop(u, None)
                closed.append(u)
                log(f"兜底结束（{'有人恢复' if others_back else '超过 4 小时'}），关闭 {u}")
        elif expired and u in want:
            forced.pop(u, None)     # 已经被 policy 收编，交回去

    st["forced_until"] = forced
    try:
        STATE.write_text(json.dumps(st))
    except Exception:
        pass

    is_on = any(on_map.get(u) for u in forced)
    mode = "全忙兜底" if (resting_all and opened) else ("兜底中" if forced else "待命")
    (DIR / "valve.json").write_text(json.dumps({
        "generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "reserve": opened or (list(forced)[0] if forced else "—"),
        "on": is_on,
        "mode": mode,
        "forced": sorted(forced),
        "seen": seen,
        "active": active,
        "resting_all": resting_all,
    }, ensure_ascii=False), encoding="utf-8")
    log(f"{seen}；模式={mode}；临时开 {sorted(forced)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
