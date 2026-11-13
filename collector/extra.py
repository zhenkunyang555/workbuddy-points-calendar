#!/usr/bin/env python3
"""查还有哪些"能领但没领"的入口：只读，不点任何领取。

覆盖成长中心里我还没自动化到的接口：
  /profile        个人资料完善度（可能带一次性奖励）
  /badges         徽章（可能有奖励）
  /energy         能量余额
  /lottery/summary 抽奖汇总（总次数/已用）
  /buddy/info /buddy/first /buddy/visible /buddy/agreement  领养与可见性
输出 /app/extra.json，供页面展示。
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

PATHS = [
    ("profile", "/profile"),
    ("badges", "/badges"),
    ("energy", "/energy"),
    ("lottery", "/lottery/summary"),
    ("buddy_info", "/buddy/info"),
    ("buddy_first", "/buddy/first"),
    ("buddy_visible", "/buddy/visible"),
    ("buddy_agreement", "/buddy/agreement"),
]


def log(line):
    with (OUT / "guard.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [extra] {line}\n")


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
        except Exception:
            pass
    return found


def get(auth, path):
    h = {"Accept": "application/json", "Authorization": "Bearer " + str(auth.get("access") or ""),
         "X-User-Id": str(auth.get("uid") or ""), "User-Agent": "WorkBuddy"}
    if auth.get("domain"):
        h["X-Domain"] = str(auth["domain"])
    req = urllib.request.Request(BASE + path, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


def brief(v, depth=0):
    """结构摘要，不打印敏感值。"""
    if isinstance(v, dict):
        if depth >= 2:
            return "{" + ",".join(list(v.keys())[:8]) + "}"
        return {k: brief(x, depth + 1) for k, x in list(v.items())[:12]}
    if isinstance(v, list):
        return {"len": len(v), "item": brief(v[0], depth + 1) if v else None}
    return v


def main():
    accs = tokens()
    out = {"generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), "accounts": []}
    dbg = {}
    try:
        pa = json.loads((CFG.parent / "plugin-auth.json").read_text(encoding="utf-8"))
        dbg["plugin_auth_keys"] = sorted(pa.keys()) if isinstance(pa, dict) else type(pa).__name__
        if isinstance(pa, dict):
            dbg["plugin_auth_fields"] = {k: sorted(v.keys()) if isinstance(v, dict) else str(type(v))
                                         for k, v in list(pa.items())[:6]}
    except Exception as e:
        dbg["plugin_auth_error"] = str(e)
    try:
        lj = json.loads((CFG.parent / "logins.json").read_text(encoding="utf-8"))
        rows = lj if isinstance(lj, list) else list(lj.values())
        dbg["logins_count"] = len(rows)
        dbg["logins_users"] = [r.get("user") for r in rows if isinstance(r, dict)]
        dbg["logins_has_auth"] = [bool(r.get("auth")) for r in rows if isinstance(r, dict)]
    except Exception as e:
        dbg["logins_error"] = str(e)
    dbg["tokens_found"] = sorted(accs.keys())
    out["debug"] = dbg
    for user, auth in accs.items():
        rec = {"user": user, "probes": {}}
        for name, path in PATHS:
            st, body = get(auth, path)
            data = body.get("data") if isinstance(body, dict) and "data" in body else body
            rec["probes"][name] = {"status": st, "shape": brief(data)}
            log(f"{user} {name} → {st}")
        # 徽章明细：页面要用它给用户列出"还差哪几件事"
        try:
            st, body = get(auth, "/badges")
            data = (body.get("data") if isinstance(body, dict) else None) or {}
            rec["badges"] = [{"code": b.get("code"), "title": b.get("title"),
                              "earned": bool(b.get("earned")), "claimed": bool(b.get("claimed"))}
                             for b in (data.get("badges") or []) if isinstance(b, dict)]
        except Exception:
            rec["badges"] = []
        try:
            st, body = get(auth, "/profile")
            data = (body.get("data") if isinstance(body, dict) else None) or body or {}
            rec["profile"] = {"completed": data.get("completed"), "total": data.get("total"),
                              "level_name": data.get("level_name")}
            rec["token_status"] = "正常" if st == 200 else (
                f"被拒（HTTP {st}）" if st else "超时·没连上")
        except Exception as e:
            rec["profile"] = {}
            rec["token_status"] = f"出错：{e}"
        try:
            st, body = get(auth, "/energy")
            data = (body.get("data") if isinstance(body, dict) else None) or body or {}
            rec["energy"] = data.get("balance")
        except Exception:
            rec["energy"] = None
        # 签到状态（看新账号为什么没签上）
        try:
            h = {"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": "Bearer " + str(auth.get("access") or ""),
                 "X-User-Id": str(auth.get("uid") or ""), "User-Agent": "WorkBuddy"}
            if auth.get("domain"):
                h["X-Domain"] = str(auth["domain"])
            req = urllib.request.Request(
                "https://copilot.tencent.com/v2/billing/meter/checkin-activity-status",
                data=b"{}", headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                b = json.loads(r.read().decode("utf-8", "replace") or "{}")
            d = (b.get("data") if isinstance(b, dict) else None) or {}
            rec["checkin"] = {"status": st2 if (st2 := r.status) else 0,
                              "active": d.get("active"), "today_checked_in": d.get("today_checked_in"),
                              "daily_credit": d.get("daily_credit"), "today_credit": d.get("today_credit"),
                              "code": (b or {}).get("code"), "msg": (b or {}).get("msg")}
        except Exception as e:
            rec["checkin"] = {"error": str(e)[:120]}
        out["accounts"].append(rec)
    (OUT / "extra.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
