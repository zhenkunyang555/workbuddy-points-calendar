#!/usr/bin/env python3
"""领徽章奖励：把「已达成但没领」的徽章逐个领掉。

2026-10-31 摸清的真实接口（之前猜的 /badges/claim 之类全是不存在的路由）：
  GET  /v2/activity/growth/badges                       徽章墙（每条带 code/record_id/earned/claimed/claimable）
  POST /v2/activity/growth/badge-records/{record_id}/claim  {"record_id": 123, "code": "chat_5"}
       → 200 {"code":0,...}；领完再读 /badges，该条 claimed 变 true

几个坑：
  · 路由按方法匹配，用 GET 探一个只收 POST 的路径会返回 “404 page not found”，
    所以 404 不代表路由不存在——得用 POST 探（错了也无副作用）。
  · 徽章里的 claimable 一直是 false，但不影响领取，真正看的是 earned && !claimed。
  · record_id 为 0 的是还没达成的，跳过。
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CFG = Path("/cfg/magpie/providers.json")
OUT = Path("/app")
BASE = "https://copilot.tencent.com/v2/activity/growth"
CST = timezone(timedelta(hours=8))


def log(s):
    with (OUT / "guard.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [claim] {s}\n")


def tokens():
    found = {}

    def walk(o, path):
        if isinstance(o, dict):
            if isinstance(o.get("access"), str) and o.get("access"):
                found[str(o.get("accountId") or path)] = o
                return
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, path)

    p = CFG.parent / "plugin-auth.json"
    if p.exists():
        try:
            walk(json.loads(p.read_text(encoding="utf-8")), "?")
        except Exception as e:
            log(f"令牌文件解析失败：{e}")
    return found


def call(auth, method, path, body=None):
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "Authorization": "Bearer " + str(auth.get("access") or ""),
         "X-User-Id": str(auth.get("uid") or ""), "User-Agent": "WorkBuddy"}
    if auth.get("domain"):
        h["X-Domain"] = str(auth["domain"])
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw.strip() else {})
        except Exception:
            return e.code, {"raw": raw[:150]}
    except Exception as e:
        return 0, {"error": str(e)}


def msg(b):
    if not isinstance(b, dict):
        return str(b)[:80]
    return str(b.get("msg") or b.get("message") or b.get("error") or b.get("raw") or "")[:80]


def badges_of(auth):
    st, body = call(auth, "GET", "/badges")
    data = (body.get("data") if isinstance(body, dict) else None) or {}
    return st, (data.get("badges") or [])


def main():
    accs = tokens()
    out = {"generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), "accounts": []}
    for user, auth in accs.items():
        st, badges = badges_of(auth)
        todo = [b for b in badges if isinstance(b, dict) and b.get("earned")
                and not b.get("claimed") and int(b.get("record_id") or 0) > 0]
        rec = {"user": user, "token_status": "正常" if st == 200 else f"被拒（HTTP {st}）",
               "pending": [b.get("code") for b in todo], "claimed": [], "failed": []}
        if st != 200:
            log(f"{user} 读徽章失败：HTTP {st}")
            out["accounts"].append(rec)
            continue
        for b in todo:
            code, rid = b.get("code"), int(b.get("record_id") or 0)
            s, rb = call(auth, "POST", f"/badge-records/{rid}/claim",
                         {"record_id": rid, "code": code})
            ok = 200 <= s < 300 and not (isinstance(rb, dict) and rb.get("code"))
            if ok:
                rec["claimed"].append(code)
                log(f"{user} 领到徽章「{b.get('title') or code}」")
            else:
                rec["failed"].append({"code": code, "status": s, "msg": msg(rb)})
                log(f"{user} 领徽章 {code} 失败：{s} {msg(rb)}")
        if not rec["claimed"]:
            left = [b.get("code") for b in badges if isinstance(b, dict)
                    and b.get("earned") and not b.get("claimed")]
            log(f"{user} 本轮没领到（待领 {len(left)} 个：{left or '无'}）")
        out["accounts"].append(rec)
    (OUT / "claim.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(a["claimed"]) for a in out["accounts"])
    log(f"本轮共领 {n} 个徽章")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
