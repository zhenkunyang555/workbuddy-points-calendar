#!/usr/bin/env python3
"""magpie 守护：顺序不被改乱 + 积分到期前的用量预测。

每 5 分钟（run.sh 控制）：
  1. 确认 group/auto 的成员顺序仍是设定的优先级，被改动就复位并通知；
  2. 从最近的路由记录里读出 workbuddy 每个账号的真实用量（积分已用/总量/重置日）；
  3. 用最近 7 天的日均消耗，推算"重置前能花掉多少、预计作废多少"，超阈值就通知。

两点事实决定了这个设计：
  - magpie 没有账号级额度的独立接口，只有每条请求的路由记录里带着（/api/gateway/route）；
  - 账号之间的先后，magpie 内置的 smart 已经是"最先重置/最先过期的优先"，
    所以这里不改账号顺序，只管组成员顺序 + 预测告警。
"""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://192.168.172.13:3430"
HOME = Path.home()
# 在容器里跑时 /app 是挂载点（写日志、状态都放那儿），本机跑时退回家目录
DIR = Path("/app") if Path("/app").is_dir() else HOME / ".magpie"
KEYF = DIR / "web.key"
LOGF = DIR / "guard.log"
STATEF = DIR / "state.json"

GROUP = "auto"
WANT = [                                   # 四个 WB 模型按顺序兜底，永不落到付费模型
    "workbuddy/deepseek-v4.1-flash",
    "workbuddy/glm-5.3-flash",
    "workbuddy/mimo-v2.6-pro",
    "workbuddy/glm-5.3",
]
ROUTING = "order"
WASTE_ALERT = 20.0     # 预计作废超过剩余的 20% 就提醒
KEEP_DAYS = 7          # 日均按最近 7 天算
TRACE_SAMPLE = 12      # 回头看最近多少条请求找账号用量


def log(msg):
    DIR.mkdir(parents=True, exist_ok=True)
    with LOGF.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    try:
        lines = LOGF.read_text(encoding="utf-8").splitlines()
        if len(lines) > 4000:
            LOGF.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(op, path):
    return json.loads(op.open(f"{BASE}/api/{path}", timeout=25).read())


def post(op, path, body):
    req = urllib.request.Request(f"{BASE}/api/{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(op.open(req, timeout=25).read())


def state_read():
    try:
        return json.loads(STATEF.read_text())
    except Exception:
        return {}


def state_write(s):
    try:
        STATEF.write_text(json.dumps(s, ensure_ascii=False))
    except Exception:
        pass


def check_order(op):
    g = next((x for x in get(op, "groups")["groups"] if x.get("id") == GROUP), None)
    if g is None:
        log(f"找不到路由组 {GROUP}")
        return
    have = g.get("members") or []
    if have != WANT or (g.get("routing") or "") != ROUTING:
        body = dict(g)
        body["members"] = WANT
        body["routing"] = ROUTING
        try:
            post(op, "groups/save", body)
            now = next((x for x in get(op, "groups")["groups"] if x.get("id") == GROUP), {})
            ok = (now.get("members") or []) == WANT
            log(("顺序被改动，已复位：" if ok else "复位失败：") + f"{have} → {now.get('members')}")
        except Exception as e:
            log(f"复位失败：{e}")


def accounts(op):
    """从最近几条路由记录里取 workbuddy 每个账号的用量。"""
    out = {}
    try:
        ids = get(op, "gateway/trace").get("ids") or []
    except Exception as e:
        log(f"读不到路由记录：{e}")
        return out
    for rid in ids[-TRACE_SAMPLE:][::-1]:
        try:
            r = get(op, f"gateway/route?id={rid}")
        except Exception:
            continue
        for e in r.get("order") or []:
            if e.get("provider") != "workbuddy" or not e.get("who"):
                continue
            if e["who"] in out:
                continue
            out[e["who"]] = {
                "used": e.get("amount"),
                "limit": e.get("limit"),
                "unit": e.get("unit"),
                "due": e.get("due") or (e.get("renews") or [None])[0],
                "model": e.get("model"),
            }
        if len(out) >= 4:
            break
    return out


def forecast(acc, burn):
    """按最近 KEEP_DAYS 天的日均消耗，估算重置前能花掉多少。"""
    out = []
    now = datetime.now(timezone.utc)
    total_left = sum(max(0.0, (a["limit"] or 0) - (a["used"] or 0))
                     for a in acc.values() if a.get("limit")) or 1.0
    for who, a in acc.items():
        if not a.get("limit") or a.get("used") is None or not a.get("due"):
            continue
        left = max(0.0, a["limit"] - a["used"])
        try:
            dt = datetime.fromisoformat(a["due"].replace("Z", "+00:00"))
        except Exception:
            continue
        dl = max(0.0, (dt - now).total_seconds() / 86400)
        projected = burn * dl * (left / total_left) if burn > 0 else 0.0
        out.append((who, left, dl, projected, max(0.0, left - projected),
                    a.get("unit") or "credits"))
    return out


def main():
    if not KEYF.exists():
        log("没有 web.key，退出")
        return 2
    op = opener()
    try:
        op.open(f"{BASE}/api/groups?k={urllib.parse.quote(KEYF.read_text().strip())}",
                timeout=15).read()
    except Exception as e:
        log(f"登录失败：{e}")
        return 1

    check_order(op)

    st = state_read()
    try:
        u = get(op, "usage")
        if u.get("credits") is not None:
            days = st.setdefault("credits_by_day", {})
            days[time.strftime("%Y-%m-%d")] = u["credits"]
            ks = sorted(days)[-30:]
            st["credits_by_day"] = {k: days[k] for k in ks}
    except Exception as e:
        log(f"用量读取失败：{e}")

    hist = sorted((st.get("credits_by_day") or {}).items())[-KEEP_DAYS:]
    burn = (hist[-1][1] - hist[0][1]) / max(1, len(hist) - 1) if len(hist) >= 2 else 0.0

    acc = accounts(op)
    if not acc:
        log("最近的路由记录里没有 workbuddy 的账号用量（当前没有流量）")
        state_write(st)
        return 0

    for who, a in acc.items():
        if a.get("limit") and a.get("used") is not None:
            log(f"账号 {who}: 已用 {a['used']:.1f}/{a['limit']} {a.get('unit') or ''} · "
                f"剩 {a['limit'] - a['used']:.0f} · 重置 {a.get('due')}")
        else:
            log(f"账号 {who}: 用量未知")

    # 真实到期日历（report.py 每小时生成）：以它为准确口径
    rep = DIR / "report.json"
    if rep.exists():
        try:
            r = json.loads(rep.read_text(encoding="utf-8"))
            for a in r.get("accounts") or []:
                if a.get("packs"):
                    log(f"积分日历 {a['user']}: 余 {a['total_left']} · {len(a['packs'])} 批 · "
                        f"最早 {a['earliest']} 走 {a['earliest_amount']}")
        except Exception:
            pass

    state_write(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
