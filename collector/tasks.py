#!/usr/bin/env python3
"""WorkBuddy 成长中心：派猫旅行 / 开盲盒 / 任务中心 / 连登兑换 / 断签补领。

全部走官方接口，令牌来自 magpie 的配置（只读挂载 /cfg），不落盘、不外传。
设计原则：只做服务端明确说"现在可以做"的事；不可逆的动作（抽奖、开 Buddy、补登）
每轮最多一次，跑多少遍都不会重复消耗。

接口（2026-10 实测，与官方成长中心 H5 一致）：
  GET  /v2/activity/growth/buddy/travel/status   状态 idle|traveling|arrived
  POST /v2/activity/growth/buddy/travel/claim    {"record_id"}
  GET  /v2/activity/growth/buddy/travel/config   可去的地点
  POST /v2/activity/growth/buddy/travel/depart   {"location_id"}
  GET  /v2/activity/growth/tasks                 任务清单
  POST /v2/activity/growth/tasks/accept          {"task_codes": [...]}
  POST /v2/activity/growth/tasks/{code}/claim    {}
  GET  /v2/activity/growth/lottery/chances       抽奖次数
  POST /v2/activity/growth/lottery/draw          {"client_token"}
  GET  /v2/activity/growth/streak                连登状态、补登卡余额
  GET  /v2/activity/growth/heatmap               活跃日历（找断签日）
  POST /v2/activity/growth/makeup-cards/use      {"target_date"}
  GET  /v2/activity/growth/redeem/summary        三档兑换状态
  POST /v2/activity/growth/redeem                {"tier","client_token"}
  GET  /v2/activity/growth/buddy/quota           能量够开几个
  POST /v2/activity/growth/buddy/open            {"count","client_token"}
"""
import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CFG = Path("/cfg/magpie/providers.json")
OUT = Path("/app")
SITE = "https://copilot.tencent.com"
BASE = SITE + "/v2/activity/growth"
APP = "5.9.3"
CST = timezone(timedelta(hours=8))
UA = "WorkBuddy"

REDEEM_TIERS = (("7d", "starter_status", "入门 7 天"),
                ("14d", "advanced_status", "进阶 14 天"),
                ("28d", "legendary_status", "巅峰 28 天"))
LAUNCH_FLOOR = date(2026, 6, 17)


def log(line):
    with (OUT / "guard.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [tasks] {line}\n")


def dig(obj, key):
    """在可能被 data/result 包裹的响应里找字段。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            v = obj.get(k)
            if isinstance(v, dict):
                r = dig(v, key)
                if r is not None:
                    return r
    return None


def as_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def client_token():
    return "u-" + str(uuid.uuid4())


def headers(auth):
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "Authorization": "Bearer " + str(auth.get("access") or ""),
         "X-User-Id": str(auth.get("uid") or ""), "User-Agent": UA}
    if auth.get("domain"):
        h["X-Domain"] = str(auth["domain"])
    return h


def call(auth, method, path, body=None, timeout=20):
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers(auth), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw) if raw.strip() else {}
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


def ok(status, body):
    return 200 <= status < 300 and as_int(dig(body, "code"), 0) in (0, None)


def msg_of(body):
    return str(dig(body, "msg") or dig(body, "message") or dig(body, "error") or "")


def tokens():
    """从 magpie 的 plugin-auth.json 里取每个账号的登录令牌。"""
    found = {}

    def walk(o, path):
        if isinstance(o, dict):
            if isinstance(o.get("access"), str) and o.get("access"):
                who = o.get("accountId") or path
                found[str(who)] = o
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


def makeup_dates(streak, heat):
    """当月、今天之前、活动开始之后、日历里 score==0 的日期，扣掉已补登的。"""
    today_raw = dig(heat, "today") or {}
    t = str(today_raw.get("date") or "")
    try:
        today = date.fromisoformat(t)
    except Exception:
        return []
    launch = str(dig(streak, "launch_date") or "")
    try:
        floor = max(today.replace(day=1), date.fromisoformat(launch), LAUNCH_FLOOR)
    except Exception:
        floor = max(today.replace(day=1), LAUNCH_FLOOR)
    st = dig(streak, "streak") or {}
    made = set()
    for d in (st.get("makeup_dates") if isinstance(st, dict) else None) or []:
        try:
            made.add(date.fromisoformat(str(d)))
        except Exception:
            pass
    out = []
    for cell in dig(heat, "cells") or []:
        if not isinstance(cell, dict):
            continue
        try:
            d = date.fromisoformat(str(cell.get("date")))
        except Exception:
            continue
        if as_int(cell.get("score"), 0) != 0:
            continue
        if floor <= d < today and d not in made:
            out.append(d.isoformat())
    return sorted(out)


def run_account(user, auth):
    """跑一遍这个账号的全部活动，返回做了什么。"""
    acts = []

    def note(s):
        acts.append(s)
        log(f"{user}: {s}")

    # 0 · 每日签到（幂等：服务端说今天已签就跳过；新账号不会被漏掉）
    st, body = call(auth, "POST", SITE + "/v2/billing/meter/checkin-activity-status", {})
    if ok(st, body):
        active = bool(dig(body, "active"))
        checked = bool(dig(body, "today_checked_in") if dig(body, "today_checked_in") is not None
                        else dig(body, "todayCheckedIn"))
        if not active:
            note("签到活动未开启")
        elif checked:
            pass                      # 已签，静默
        else:
            c, b = call(auth, "POST", SITE + "/v2/billing/meter/daily-checkin", {})
            if ok(c, b):
                note(f"签到 +{as_int(dig(b, 'credit'))} 分")
            else:
                note(f"签到失败：{msg_of(b) or c}")
    elif st in (401, 403):
        note(f"令牌被拒（{st}），跳过")
        return acts

    # 1 · 旅行：到站先领礼物
    st, body = call(auth, "GET", "/buddy/travel/status")
    travel, daily_limit, rid = None, False, None
    if ok(st, body):
        travel = str(dig(body, "state") or "")
        daily_limit = bool(dig(body, "daily_limit_reached"))
        rid = dig(body, "record_id")
        if travel == "arrived" and rid:
            c, b = call(auth, "POST", "/buddy/travel/claim", {"record_id": rid})
            if ok(c, b):
                note(f"领旅行礼物 +{as_int(dig(b, 'reward_credit'))} 分")
                travel = "idle"
            else:
                note(f"领旅行礼物失败：{msg_of(b) or c}")
                travel = "arrived"      # 没领到就别覆盖
        elif travel == "traveling":
            note("猫还在旅行中")
    elif st in (401, 403):
        note(f"令牌被拒（{st}），跳过")
        return acts

    # 2 · 任务中心：接单 + 领奖
    st, body = call(auth, "GET", "/tasks")
    if ok(st, body):
        tasks = dig(body, "tasks") or []
        titles = {t.get("task_code"): t.get("title") for t in tasks if isinstance(t, dict)}
        pending = [t.get("task_code") for t in tasks
                   if isinstance(t, dict) and t.get("task_code")
                   and not t.get("locked") and t.get("accept_status") == "not_accepted"]
        for i in range(0, len(pending), 20):
            c, b = call(auth, "POST", "/tasks/accept", {"task_codes": pending[i:i + 20]})
            if ok(c, b):
                got = dig(b, "results")
                n = len(got) if isinstance(got, list) else len(pending[i:i + 20])
                note(f"接下 {n} 个任务")
            else:
                note(f"接任务失败：{msg_of(b) or c}")
        done = [t.get("task_code") for t in tasks
                if isinstance(t, dict) and t.get("task_code")
                and t.get("accept_status") == "completed"]
        for code in done[:10]:
            c, b = call(auth, "POST", f"/tasks/{code}/claim", {})
            if ok(c, b):
                note(f"领任务奖励「{titles.get(code) or code}」")
            else:
                note(f"领任务奖励失败（{titles.get(code) or code}）：{msg_of(b) or c}")

    # 3 · 断签补领：先补，再兑换（补登会改变连签天数）
    streak = None
    st, body = call(auth, "GET", "/streak")
    if ok(st, body):
        streak = body
        cards_obj = dig(body, "makeup_cards")
        cards = as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) else as_int(cards_obj)
        if cards > 0:
            h, hb = call(auth, "GET", "/heatmap")
            if ok(h, hb):
                days = makeup_dates(body, hb)
                if days:
                    d = days[0]                       # 每轮最多补一天，稳妥
                    c, b = call(auth, "POST", "/makeup-cards/use", {"target_date": d})
                    if ok(c, b):
                        left = dig(b, "makeup_cards")
                        left = as_int(left.get("balance")) if isinstance(left, dict) else as_int(left)
                        note(f"补登 {d}（剩 {left} 张卡）")
                        streak = None                 # 连签变了，兑换前重新取
                    elif "no makeup needed" in msg_of(b).lower():
                        note(f"{d} 无需补登")
                    else:
                        note(f"补登 {d} 失败：{msg_of(b) or c}")

    # 4 · 连登兑换（三档）
    st, body = call(auth, "GET", "/redeem/summary")
    if ok(st, body):
        for tier, key, label in REDEEM_TIERS:
            status = str(dig(body, key) or "")
            if not status or status in ("claimed", "locked"):
                continue
            c, b = call(auth, "POST", "/redeem", {"tier": tier, "client_token": client_token()})
            if c == 403:
                note(f"连登兑换「{label}」未解锁")
                continue
            if ok(c, b):
                bits = [f"+{as_int(dig(b, k))} {n}" for k, n in
                        (("credit_granted", "分"), ("energy_granted", "能量"),
                         ("cards_granted", "补登卡"), ("chances_granted", "次抽奖"))
                        if as_int(dig(b, k))]
                note(f"连登兑换「{label}」" + ("（" + " ".join(bits) + "）" if bits else ""))
            else:
                note(f"连登兑换「{label}」失败：{msg_of(b) or c}")

    # 5 · 开盲盒（抽奖）
    st, body = call(auth, "GET", "/lottery/chances")
    if ok(st, body):
        chances = as_int(dig(body, "balance"))
        if chances > 0:
            c, b = call(auth, "POST", "/lottery/draw", {"client_token": client_token()})
            if ok(c, b):
                prize = str(dig(b, "prize_name") or dig(b, "prize") or "未知")
                if dig(b, "need_address") or dig(b, "require_address"):
                    prize += "（实物奖，要去成长中心填地址）"
                note(f"开盲盒：{prize}" + (f"，还剩 {chances - 1} 次" if chances > 1 else ""))
            else:
                note(f"开盲盒失败：{msg_of(b) or c}")

    # 6 · 开 Buddy 盲盒（能量够就开）
    if travel == "idle":
        st, body = call(auth, "GET", "/buddy/quota")
        if ok(st, body):
            afford = as_int(dig(body, "affordable"))
            cap = as_int(dig(body, "max_open_count"), 1) or 1
            if afford > 0:
                n = min(afford, cap)
                c, b = call(auth, "POST", "/buddy/open",
                            {"count": n, "client_token": client_token()})
                if ok(c, b):
                    note(f"开 Buddy 盲盒 ×{n}")
                else:
                    note(f"开 Buddy 失败：{msg_of(b) or c}")

        # 7 · 派出（当天名额没用完才派）
        if not daily_limit:
            st, body = call(auth, "GET", "/buddy/travel/config")
            if ok(st, body):
                locs = dig(body, "locations") or []
                if isinstance(locs, list) and locs and isinstance(locs[0], dict):
                    c, b = call(auth, "POST", "/buddy/travel/depart",
                                {"location_id": locs[0].get("id")})
                    if ok(c, b):
                        loc = dig(b, "location") or {}
                        name = loc.get("name") if isinstance(loc, dict) else ""
                        note(f"派出猫去{name or '目的地'}"
                             f"（{dig(b, 'duration_hours') or '?'} 小时）")
                    elif "no active buddy" in msg_of(b).lower():
                        note("还没领养猫，跳过派出")
                    else:
                        note(f"派出失败：{msg_of(b) or c}")
    return acts


def main():
    accounts = tokens()
    if not accounts:
        log("没有拿到令牌，跳过")
        return 1
    report = {"generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), "accounts": []}
    for user, auth in accounts.items():
        try:
            acts = run_account(user, auth)
        except Exception as e:
            acts = [f"异常：{type(e).__name__}: {e}"]
            log(f"{user}: {acts[0]}")
        report["accounts"].append({"user": user, "actions": acts})
    (OUT / "tasks.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
