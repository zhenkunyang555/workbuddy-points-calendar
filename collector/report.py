#!/usr/bin/env python3
"""WorkBuddy 积分到期日历：把每个账号每一批积分的到期日全拉下来。

数据来源（WorkBuddy 官方计费口，与 magpie 的插件同一套）：
  /billing/meter/get-user-resource-summary
  /billing/meter/get-user-resource-free-packages
  /billing/meter/get-user-resource-paid-packages

令牌从 magpie 自己的配置里读（只读挂载 /cfg），因为只有 magpie 手里有登录令牌。
输出：/app/report.json（机器读）+ /app/report.html（人看）
"""
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

CFG = Path("/cfg/magpie/providers.json")
OUT = Path("/app")
SITE = "https://copilot.tencent.com"
APP = "5.9.3"
SLICE = "4"           # CapacityType 4 = 每日切片，不算积分包
CST = timezone(timedelta(hours=8))

PATHS = [
    ("summary", "/billing/meter/get-user-resource-summary"),
    ("free", "/billing/meter/get-user-resource-free-packages"),
    ("paid", "/billing/meter/get-user-resource-paid-packages"),
]

# WorkBuddy 客户端列积分包时用的品类码（接口不给就报 10001 PackageCodes required）
CODE = lambda n, s: f"TCACA_code_{n}_{s}"
DAILY_CODE = CODE("001", "PqouKr6QWV")
PAID_CODES = [CODE("002", "AkiJS3ZHF5"), CODE("005", "maRGyrHhw1"), CODE("003", "FAnt7lcmRT"),
              CODE("023", "4xbGhMrE6q"), CODE("026", "BaESVICNoi"), CODE("027", "0FCGVA6vSa"),
              CODE("009", "0XmEQc2xOf"), CODE("038", "OhvqZtiPKr"), CODE("036", "lupO5WgNdG")]
FREE_CODES = [DAILY_CODE, CODE("008", "cfWoLwvjU4"), CODE("035", "ArVxJcGDsm"),
              CODE("006", "DbXS0lrypC"), CODE("039", "KRcQj7wUat"), CODE("040", "mi9rCYg46x"),
              CODE("007", "nzdH5h4Nl0"), CODE("028", "NtpWi0jzXs"), CODE("037", "WxOD3MpI2o"),
              CODE("029", "6wCGEWquYy"), CODE("030", "BjSt89qTvr")]


def log(msg):
    with (OUT / "guard.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [report] {msg}\n")


def num(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def end_ms(p):
    """一批积分的失效时间（毫秒）。"""
    for k in ("DeductionEndTime",):
        v = p.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    for k in ("ExpiredTime", "CycleEndTime"):
        v = p.get(k)
        if not isinstance(v, str) or not v.strip():
            continue
        s = v.strip()
        try:
            if len(s) == 19 and s[4] == "-" and s[10] == " ":
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            else:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CST)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return 0


def ask(auth, path, body=None):
    uid = auth.get("uid") or ""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        SITE + path, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"WorkBuddy/{APP}",
            "Authorization": "Bearer " + str(auth.get("access") or ""),
            "X-User-Id": str(uid),
            "X-Domain": str(auth.get("domain") or "copilot.tencent.com"),
            "X-Product": "SaaS",
            "X-IDE-Type": "WorkBuddy",
        })
    with urllib.request.urlopen(req, timeout=25) as r:
        env = json.loads(r.read().decode("utf-8", "replace") or "{}")
    code = int(env.get("code") or 0)
    if code:
        raise RuntimeError(f"code={code} {env.get('msg') or ''}")
    return env.get("data")


def shapes():
    """只报结构，不碰具体值。"""
    out = {}
    for name in ("logins.json", "plugin-auth.json", "stash.json", "quotas.json",
                 "credits-daily.json", "balance-history.json"):
        p = CFG.parent / name
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            out[name] = f"parse error: {e}"
            continue
        if isinstance(d, dict):
            out[name] = {"top_keys": sorted(d.keys())[:12]}
            for k, v in list(d.items())[:3]:
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    out[name][f"sample[{k}]"] = sorted(v[0].keys())
                elif isinstance(v, dict):
                    out[name][f"sample[{k}]"] = sorted(v.keys())[:14]
        elif isinstance(d, list):
            out[name] = {"len": len(d),
                         "sample": sorted(d[0].keys()) if d and isinstance(d[0], dict) else type(d[:1]).__name__}
            if name == "logins.json":
                out[name]["rows"] = [
                    {"user": r.get("user"), "agent": r.get("agent"),
                     "auth_keys": sorted((r.get("auth") or {}).keys()) if isinstance(r.get("auth"), dict) else str(type(r.get("auth")))}
                    for r in d if isinstance(r, dict)]
    return out


def walk_auths(o, path, found):
    if isinstance(o, dict):
        if o.get("access") and isinstance(o.get("access"), str):
            who = o.get("accountId") or o.get("uid") or path
            found[str(who)] = o
            return
        for k, v in o.items():
            walk_auths(v, f"{path}/{k}", found)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            walk_auths(v, f"{path}[{i}]", found)


def save_tokens():
    """从 logins.json 里找 workbuddy 的登录令牌（文件是列表，元素含 user/auth）。"""
    found = {}
    for name in ("plugin-auth.json", "stash.json", "logins.json"):
        lp = CFG.parent / name
        if not lp.exists():
            continue
        try:
            walk_auths(json.loads(lp.read_text(encoding="utf-8")), name, found)
        except Exception:
            continue
    return found


def probe(auth):
    """看接口返回的形状（不打印具体值）。"""
    out = {}
    day = datetime.now(CST).strftime("%Y-%m-%d")
    lists = [
        ("paid", PATHS[2][1], {"PackageCodes": PAID_CODES, "NeedRenewInfo": True}),
        ("free", PATHS[1][1], {"PackageCodes": FREE_CODES,
                               "SlicePeriodStartTime": day + " 00:00:00",
                               "SlicePeriodEndTime": day + " 23:59:59"}),
    ]
    for kind, path, extra in lists:
        try:
            d = ask(auth, path, {"PageNumber": 1, "PageSize": 200, "Status": [0, 3], **extra})
        except Exception as e:
            out[kind] = f"error: {e}"
            continue
        info = {"type": type(d).__name__}
        if isinstance(d, dict):
            info["keys"] = sorted(d.keys())[:20]
            info["TotalCount"] = d.get("TotalCount")
            accs = d.get("Accounts") or []
            info["accounts_len"] = len(accs)
            if accs and isinstance(accs[0], dict):
                info["account_keys"] = sorted(accs[0].keys())[:20]
                pk = accs[0].get("Packages") or []
                info["packages_len"] = len(pk)
                if pk and isinstance(pk[0], dict):
                    info["package_keys"] = sorted(pk[0].keys())
        out[kind] = info
    return out


def tokens():
    """{账号: auth 字典}，来自 magpie 的配置。"""
    dbg = {"shapes": shapes()}
    found = save_tokens()
    dbg["tokens_found"] = sorted(found.keys())
    try:
        (OUT / "debug.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=1))
    except Exception:
        pass
    if found:
        return found
    if not CFG.exists():
        log(f"读不到 {CFG}")
        return {}
    try:
        raw = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"providers.json 解析失败：{e}")
        return {}
    out = {}
    items = raw.get("providers") if isinstance(raw, dict) else raw
    if isinstance(items, dict):
        items = list(items.values())
    for p in items or []:
        if not isinstance(p, dict):
            continue
        for lg in ((p.get("account") or {}).get("logins") or []):
            a = lg.get("auth") or {}
            if a.get("access"):
                out[lg.get("user") or a.get("uid") or "?"] = a
    return out


def parse_pack(p, kind, counted, now):
    """一个积分包 → 一行记录；没有余额或已过期的返回 None。"""
    if not isinstance(p, dict):
        return None
    if str(p.get("CapacityType")) == SLICE:      # 每日切片，不算积分包
        return None
    if p.get("PackageCode") == DAILY_CODE:
        return None
    left = num(p.get("CycleCapacityRemainPrecise")
               or p.get("CycleRemainCapacityPrecise")
               or p.get("CycleRemainCapacity")
               or p.get("CycleCapacityRemain"))
    end = end_ms(p)
    if left <= 0 or end <= now:
        return None
    return {"kind": kind, "code": p.get("PackageCode"),
            "counted": (not counted) or (p.get("PackageCode") in counted),
            "left": round(left, 2), "end": end,
            "end_local": datetime.fromtimestamp(end / 1000, CST).strftime("%Y-%m-%d %H:%M")}


def build():
    accounts = tokens()
    if not accounts:
        log("没有拿到任何 workbuddy 令牌（可能配置里是加密存的）")
        return {"generated": None, "accounts": [], "error": "no tokens"}

    now = int(time.time() * 1000)
    out = []
    for user, auth in accounts.items():
        rec = {"user": user, "packs": [], "error": None, "checkin": None}
        try:                       # 今日打卡状态：活动没开或接口报错都留 None，页面显示 —
            ck = ask(auth, "/v2/billing/meter/checkin-activity-status", {})
            active = bool((ck or {}).get("active"))
            done = bool((ck or {}).get("today_checked_in"))
            rec["checkin"] = ("已打卡" if done else "未打卡") if active else "未开放"
        except Exception as e:
            log(f"{user} 打卡状态取不到：{e}")
        try:
            sum_ = ask(auth, PATHS[0][1])
            counted = {p.get("PackageCode") for p in (sum_ or {}).get("Packages") or []}
            day = datetime.now(CST).strftime("%Y-%m-%d")
            lists = [
                ("paid", PATHS[2][1], {"PackageCodes": PAID_CODES, "NeedRenewInfo": True}),
                ("free", PATHS[1][1], {"PackageCodes": FREE_CODES,
                                       "SlicePeriodStartTime": day + " 00:00:00",
                                       "SlicePeriodEndTime": day + " 23:59:59"}),
            ]
            for kind, path, extra in lists:
                for page in range(1, 11):
                    try:
                        d = ask(auth, path, {"PageNumber": page, "PageSize": 200,
                                             "Status": [0, 3], **extra})
                    except Exception as e:
                        log(f"{user} {kind} 第 {page} 页失败：{e}")
                        break
                    got = (d or {}).get("Accounts") or []
                    for p in got:
                        row = parse_pack(p, kind, counted, now)
                        if row:
                            rec["packs"].append(row)
                    if len(got) < 200:
                        break
            rec["packs"].sort(key=lambda x: x["end"])
            rec["total_left"] = round(sum(x["left"] for x in rec["packs"]), 2)
            # 按“日历日”聚合。以前按 end_local（精确到分钟）分组，同一天不同分钟的批次
            # 被当成不同日期，于是“那天走掉”只统计了最早那一分钟的一批。
            days = {}
            for x in rec["packs"]:
                k = x["end_local"][:10]
                e = days.setdefault(k, {"date": k, "amount": 0.0, "packs": 0})
                e["amount"] += x["left"]
                e["packs"] += 1
            rec["days"] = [{"date": k, "amount": round(v["amount"], 2), "packs": v["packs"]}
                           for k, v in sorted(days.items())]
            rec["near30"] = round(sum(x["left"] for x in rec["packs"]
                                      if x["end"] <= now + 30 * 86400000), 2)
            rec["near45"] = round(sum(x["left"] for x in rec["packs"]
                                      if x["end"] <= now + 45 * 86400000), 2)
            if rec["days"]:
                d0 = rec["days"][0]
                rec["earliest"] = d0["date"]            # 只到日，不再到分钟
                rec["earliest_amount"] = d0["amount"]   # 当天所有批次的合计
                rec["earliest_packs"] = d0["packs"]
        except Exception as e:
            rec["error"] = str(e)
            log(f"{user} 汇总失败：{e}")
        out.append(rec)
    return {"generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": out}


HTML = """<!doctype html><meta charset="utf-8">
<title>WorkBuddy 积分到期日历</title>
<style>
body{{font:15px/1.6 -apple-system,"PingFang SC",sans-serif;margin:24px;color:#111}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;font-size:13px;margin-bottom:18px}}
.acct{{border:1px solid #e5e5e5;border-radius:10px;padding:14px 16px;margin-bottom:18px}}
.name{{font-weight:600;margin-bottom:6px}} .big{{font-size:22px;font-weight:600}}
table{{border-collapse:collapse;width:100%;margin-top:10px;font-size:14px}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid #f0f0f0}}
th{{color:#666;font-weight:500}} .r{{text-align:right}}
.warn{{color:#b45309}} .err{{color:#b91c1c}}
details.packs{{margin-top:2px}}
details.packs>summary{{list-style:none;cursor:pointer;text-align:right;font-size:12px;color:#2563eb;
 padding:2px 6px;border-radius:4px;user-select:none;width:max-content;margin-left:auto}}
details.packs>summary::-webkit-details-marker{{display:none}}
details.packs>summary::before{{content:"▸ "}}
details.packs[open]>summary::before{{content:"▾ "}}
details.packs>summary:hover{{background:#eff6ff}}
</style>
<h1>WorkBuddy 积分到期日历</h1>
<div class="sub" id="gen-stamp" data-gen="{gen}">生成时间 {gen} · 数据来自 WorkBuddy 计费口 · 只列还有余额且未过期的批次</div>
{body}
"""


def _today_snapshot():
    try:
        return json.loads((OUT / "today.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def today_block():
    """每日消耗卡片的下半块：今天每个账号花了多少（页面里的脚本每 5 秒刷一次）。"""
    d = _today_snapshot()
    rows = ""
    stamp = "正在取数…"
    if d:
        for a in d.get("accounts") or []:
            sh = a.get("share")
            share_txt = (f"{sh:.1f}%") if sh is not None else "—"
            rows += ("<tr><td>" + str(a.get("user")) + "</td>"
                     f'<td class="r">{a.get("today", 0):.2f}</td>'
                     f'<td class="r">{share_txt}</td>'
                     f'<td class="r" style="color:#666">{a.get("yesterday", 0):.2f}</td></tr>')
        rows += (f'<tr><td><b>合计</b></td><td class="r"><b>{d.get("total_today", 0):.2f}</b></td>'
                 f'<td class="r"></td>'
                 f'<td class="r" style="color:#666"><b>{d.get("total_yesterday", 0):.2f}</b></td></tr>')
        stamp = ("页面读数 " + str(d.get("generated"))[11:] +
                 " · 最近一次扣分 " + str(d.get("last_req") or "—"))
    if not rows:
        rows = '<tr><td colspan="4" style="color:#666">还没有今天的读数（守护进程刚重启时会空几秒）</td></tr>'
    return ('<div style="margin-top:14px;border-top:1px solid #eee;padding-top:10px">'
            '<div class="name">今天每个账号花了多少 '
            '<span id="live-stamp" style="font-weight:400;color:#666;font-size:13px">'
            + stamp + '</span></div>'
            '<table id="live-today"><thead><tr><th>账号</th><th class="r">今天消耗</th>'
            '<th class="r">占今天</th><th class="r">昨天全天</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<div id="live-note" style="color:#666;font-size:13px;margin-top:6px">'
            '每 5 秒自动刷新；数字按每次请求的扣分累加，随请求立刻变。'
            '上面那张图跟这张表同一口径（只是按天汇总），所以两边的昨天应该对得上'
            '</div>'
            '</div>')  # 关掉最外层那个 margin-top:14px 的包裹 div（以前漏了，导致后面所有卡片都被套进来）


LIVE_JS = """
<script>
(function(){
  var tb = document.querySelector('#live-today tbody');
  var stamp = document.getElementById('live-stamp');
  var note = document.getElementById('live-note');
  function m(v){ v = Number(v); return isFinite(v) ? v.toFixed(2) : '0.00'; }
  function tick(){
    fetch('today.json?t=' + Date.now(), {cache:'no-store'})
      .then(function(r){ if(!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d){
        var rows = '';
        (d.accounts || []).forEach(function(a){
          var sh = (a.share === null || a.share === undefined) ? '—' : Number(a.share).toFixed(1) + '%';
          rows += '<tr><td>' + a.user + '</td><td class="r">' + m(a.today) + '</td>' +
                  '<td class="r">' + sh + '</td>' +
                  '<td class="r" style="color:#666">' + m(a.yesterday) + '</td></tr>';
        });
        rows += '<tr><td><b>合计</b></td><td class="r"><b>' + m(d.total_today) + '</b></td>' +
                '<td class="r"></td><td class="r" style="color:#666"><b>' + m(d.total_yesterday) + '</b></td></tr>';
        if(tb) tb.innerHTML = rows;
        if(stamp) stamp.textContent = '页面读数 ' + String(d.generated || '').slice(11) +
                                      ' · 最近一次扣分 ' + (d.last_req || '—');
        if(note) note.textContent = (Number(d.total_today) > 0)
          ? '每 5 秒自动刷新；数字按每次请求的扣分累加，随请求立刻变。上面那张图跟这张表同一口径（只是按天汇总），所以两边的昨天应该对得上'
          : '每 5 秒自动刷新；今天还没有扣分记录（0 点刚过，或还没开始用）';
      })
      .catch(function(e){ if(stamp) stamp.textContent = '刷新失败（' + e.message + '），5 秒后自动重试'; });
  }
  tick();
  setInterval(tick, 5000);
  // 顶部表格（含令牌列）是生成页面时的快照，只有重载才会变。
  // 每 60 秒看一眼 report.json 的数据时间，比这一版新就整页重载，免得用户对着几小时前的旧表。
  var genEl = document.getElementById('gen-stamp');
  var GEN = genEl ? (genEl.getAttribute('data-gen') || '') : '';
  function checkGen(){
    if(!GEN) return;
    fetch('report.json?t=' + Date.now(), {cache:'no-store'})
      .then(function(r){ if(!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(j){
        if(!(j && j.generated && j.generated !== GEN)) return;
        // 防跑飞：URL 上记下「为哪一版重载过」。同一版只重载一次，
        // 就算 report.html 和 report.json 的时间对不上也不会无限刷（以前用 sessionStorage，静默失效了）。
        try {
          var u = new URL(location.href);
          if(u.searchParams.get('shown') === j.generated) return;
          u.searchParams.set('shown', j.generated);
          location.replace(u.toString());
        } catch(e){ location.reload(); }
      })
      .catch(function(){});
  }
  setTimeout(checkGen, 10000);
  setInterval(checkGen, 60000);
})();
</script>
"""


def calendar_block(data):
    """到期日程：每天会走掉多少（同一天多批已合并），45 天内的按天列，更远的折一行。"""
    accs = [a for a in (data.get("accounts") or []) if not a.get("error")]
    if not accs:
        return ""
    users = [a["user"] for a in accs]
    edge = (datetime.now(CST) + timedelta(days=45)).date()
    rows = {}
    far = {u: 0.0 for u in users}
    for a in accs:
        for d in a.get("days") or []:
            try:
                dd = datetime.strptime(str(d.get("date")), "%Y-%m-%d").date()
            except Exception:
                continue
            amt = float(d.get("amount") or 0)
            if dd <= edge:
                rows.setdefault(dd, {})[a["user"]] = rows.setdefault(dd, {}).get(a["user"], 0) + amt
            else:
                far[a["user"]] += amt
    if not rows and not any(far.values()):
        return ""
    totals = {dd: sum(v.values()) for dd, v in rows.items()}
    peak = max(totals.values()) if totals else 0
    head = "".join(f'<th class="r">{u}</th>' for u in users)
    body = ""
    for dd in sorted(rows):
        cells = "".join(
            (f'<td class="r">{rows[dd][u]:.2f}</td>' if rows[dd].get(u)
             else '<td class="r" style="color:#ccc">—</td>') for u in users)
        is_peak = totals[dd] >= peak - 1e-6
        mark = ' <span class="warn">← 峰值</span>' if is_peak else ""
        style = ' style="background:#fff7ed"' if is_peak else ""
        body += (f'<tr{style}><td>{dd.strftime("%Y-%m-%d")}{mark}</td>{cells}'
                 f'<td class="r"><b>{totals[dd]:.2f}</b></td></tr>')
    if any(far.values()):
        cells = "".join((f'<td class="r">{far[u]:.2f}</td>' if far[u]
                         else '<td class="r" style="color:#ccc">—</td>') for u in users)
        body += (f'<tr style="color:#666"><td>45 天以后（长效包）</td>{cells}'
                 f'<td class="r">{sum(far.values()):.2f}</td></tr>')
    return ('<div class="acct"><div class="name">到期日程 · 每天会走掉多少</div>'
            '<div style="color:#666;font-size:13px;margin-bottom:2px">'
            '按日历日合计（同一天的多批已合并）；只列 45 天内，更远的长效包在最后一行</div>'
            f'<table><tr><th>日期</th>{head}<th class="r">合计</th></tr>{body}</table></div>')


def render(data):
    parts = []
    blocks = {}    # 顶部几块：key = overview / history / dispatch / calendar，最后按固定顺序拼
    for a in data["accounts"]:
        if a.get("error"):
            parts.append(f'<div class="acct"><div class="name">{a["user"]}</div>'
                         f'<div class="err">读取失败：{a["error"]}</div></div>')
            continue
        rows = "".join(
            f'<tr><td>{p["end_local"]}</td><td class="r">{p["left"]:.2f}</td>'
            f'<td>{"免费" if p["kind"]=="free" else "付费"}</td>'
            f'<td>{"计入" if p["counted"] else "未计入汇总"}</td></tr>'
            for p in a["packs"])
        first = a.get("earliest") or "—"
        amt = a.get("earliest_amount") or 0
        npk = a.get("earliest_packs") or 0
        near30 = a.get("near30")
        near30 = near30 if near30 is not None else 0
        share = (near30 / a["total_left"] * 100) if a.get("total_left") else 0
        parts.append(
            f'<div class="acct"><div class="name">{a["user"]}</div>'
            f'<div class="big">{a["total_left"]:.2f} <span style="font-size:13px;color:#666">'
            f'分，共 {len(a["packs"])} 批</span></div>'
            f'<div class="warn">最早到期：{first}，当天到期 {amt:.2f} 分（{npk} 批）</div>'
            f'<div style="color:#666;font-size:13px">30 天内到期 {near30:.2f} 分（占 {share:.0f}%）'
            f' · 45 天内 {a.get("near45") or 0:.2f}</div>'
            f'<details class="packs"><summary>展开 {len(a["packs"])} 批明细</summary>'
            f'<table><tr><th>到期日</th><th class="r">还剩</th><th>来源</th><th>是否计入</th></tr>'
            f'{rows}</table></details></div>')
    # 顶部汇总：多个账号一眼看全
    try:
        status = {}
        ep0 = OUT / "extra.json"
        if ep0.exists():
            for a in (json.loads(ep0.read_text(encoding="utf-8")).get("accounts") or []):
                status[a.get("user")] = a.get("token_status")
        rows = ""
        for a in data.get("accounts") or []:
            ck = a.get("checkin")
            if ck == "已打卡":
                ck_html = '<span style="color:#15803d">已打卡</span>'
            elif ck == "未打卡":
                ck_html = '<span class="warn">未打卡</span>'
            else:
                ck_html = '<span style="color:#999">—</span>'
            if a.get("error"):
                rows += f'<tr><td>{a["user"]}</td><td>{ck_html}</td><td colspan="5" class="err">{a["error"]}</td></tr>'
                continue
            rows += (f'<tr><td>{a["user"]}</td><td>{ck_html}</td>'
                     f'<td class="r">{(a.get("total_left") or 0):.2f}</td>'
                     f'<td>{a.get("earliest") or "—"}</td>'
                     f'<td class="r">{(a.get("earliest_amount") or 0):.2f}</td>'
                     f'<td class="r">{(a.get("near30") or 0):.2f}</td>'
                     f'<td>{status.get(a["user"]) or "?"}</td></tr>')
        total = sum((a.get("total_left") or 0) for a in data.get("accounts") or [] if not a.get("error"))
        blocks["overview"] = ('<div class="acct"><div class="name">账号总览</div>'
                    f'<div class="big">{total:.2f} <span style="font-size:13px;color:#666">分合计</span></div>'
                    '<table><tr><th>账号</th><th>打卡</th><th class="r">剩余</th><th>最近到期日</th>'
                    f'<th class="r">那天到期</th><th class="r">30 天内</th><th>令牌</th></tr>{rows}</table></div>')
    except Exception:
        pass
    ep = OUT / "extra.json"
    if ep.exists():
        try:
            ex = json.loads(ep.read_text(encoding="utf-8"))
            rows = ""
            for a in ex.get("accounts") or []:
                bd = a.get("badges") or []
                if not bd:
                    continue
                done = sum(1 for b in bd if b.get("earned"))
                items = "".join(
                    f'<li>{"✅" if b.get("earned") else "⬜"} {b.get("title")}'
                    f'{"（已达成本次未领，需在客户端点领取）" if b.get("earned") and not b.get("claimed") else ""}</li>'
                    for b in bd)
                pf = a.get("profile") or {}
                rows += (f'<div class="name" style="margin-top:8px">{a["user"]} · 令牌 {a.get("token_status") or "?"}'
                         f' · 徽章 {done}/{len(bd)}'
                         f' · 成长计划 {pf.get("completed")}/{pf.get("total")}'
                         f' · 能量 {a.get("energy")}</div><ul>{items}</ul>')
            if rows:
                parts.append('<div class="acct"><div class="name">要你亲手点几下才能解锁的（我每天自动勾）'
                             f'</div>{rows}</div>')
        except Exception:
            pass
    bp = OUT / "budget.json"
    if bp.exists():
        try:
            b = json.loads(bp.read_text(encoding="utf-8"))
            fresh = {a["user"]: a for a in (data.get("accounts") or []) if a.get("user")}

            def pick(u, key, default=0):
                """页面上的数字以本次抓到的 report.json 为准（budget.json 是上一轮的快照）。"""
                v = fresh.get(u, {}).get(key)
                return default if v is None else v
            rows = "".join(
                f'<tr><td>{a["user"]}</td><td class="r">{pick(a["user"], "total_left", a["total"]):.0f}</td>'
                f'<td>{pick(a["user"], "earliest", a.get("next_end")) or "—"}</td>'
                f'<td class="r">{pick(a["user"], "earliest_amount", a.get("next_left")) or 0:.1f}</td>'
                f'<td class="r">{pick(a["user"], "near30", a.get("near30")) or 0:.0f}</td>'
                f'<td>{a.get("role") or ""}</td></tr>'
                for a in b.get("accounts") or [])
            chg = "；".join(b.get("changes") or []) or "无需调整"
            rot = " → ".join(b.get("rotation") or []) or "—"
            vline = ""
            vp = OUT / "valve.json"
            if vp.exists():
                try:
                    v = json.loads(vp.read_text(encoding="utf-8"))
                    vline = ('<div style="margin-top:6px">兜底（安全阀）：'
                             f'{v.get("reserve")} 当前{"<b>开启</b>" if v.get("on") else "关闭"}'
                             f' · {v.get("mode") or ""}'
                             f' · {v.get("seen")}</div>')
                except Exception:
                    pass
            burn = b.get("burn_per_day")
            thr = b.get("rotate_below") or b.get("safety_threshold")
            if thr:
                policy_line = (f'<div>日均消耗 {burn} 分　换人门槛 {thr} 分'
                               f'　<span style="color:#666;font-size:13px">轮值顺序：{rot}</span></div>')
            else:
                policy_line = ('<div>所有账号常开，签到 / 任务 / 抽奖一个不落；'
                               '登录位不参与排名，每 12 小时换人'
                               f'<span style="color:#666;font-size:13px">'
                               f' · 到期最多的 {b.get("backup") or "—"} 永远占一个排名位</span></div>')
            blocks["dispatch"] = ('<div class="acct"><div class="name">消耗调度（每 15 分钟判断一次）</div>'
                        f'<div>当前模式：<b>{b.get("mode")}</b>　登录位：<b>{b.get("target") or "—"}</b></div>'
                        + policy_line +
                        '<table><tr><th>账号</th><th class="r">余额</th><th>最近到期日</th>'
                        '<th class="r">那天到期</th><th class="r">30 天内</th><th>角色</th></tr>'
                        f'{rows}</table><div style="color:#666;font-size:13px;margin-top:6px">'
                        f'最近一次判断 {b.get("generated")} · {chg}</div>{vline}</div>')
        except Exception:
            pass
    hp = OUT / "history.json"
    if hp.exists():
        try:
            h = json.loads(hp.read_text(encoding="utf-8"))
            if h.get("svg"):
                blocks["history"] = ('<div class="acct"><div class="name">每日消耗 · 近 30 天</div>'
                                f'<div style="margin:4px 0 8px"><b>{h.get("summary")}</b>'
                                f'　<span style="color:#666;font-size:13px">'
                                f'{h.get("start") or "—"} → {h.get("end") or "—"}'
                                f'（{h.get("days") or 30} 天，有记录 {h.get("data_days") or "—"} 天）</span></div>'
                                + (f'<div style="margin:0 0 8px;color:#b45309">{h.get("forecast")}</div>'
                                   if h.get("forecast") else "")
                                + (f'<div style="margin:0 0 8px;color:#b91c1c;font-weight:600">'
                                   f'⚠ 数据可能不全：{h.get("warn")}</div>'
                                   if h.get("warn") else "") +
                                f'{h.get("svg")}'
                                '<div style="color:#666;font-size:13px;margin-top:4px">'
                                + (h.get("legend") or '灰色 = 10-30 之前　蓝色 = 10-30 及之后') + '</div>'
                                + (f'<div style="color:#666;font-size:12px;margin-top:2px">{h.get("note")}</div>'
                                   if h.get("note") else "")
                                + today_block() + '</div>')
        except Exception:
            pass
    tp = OUT / "tasks.json"
    if tp.exists():
        try:
            tk = json.loads(tp.read_text(encoding="utf-8"))
            rows = ""
            for a in tk.get("accounts") or []:
                items = "".join(f"<li>{x}</li>" for x in (a.get("actions") or ["（本轮没有可做的动作）"]))
                rows += f'<div class="name" style="margin-top:8px">{a["user"]}</div><ul>{items}</ul>'
            parts.append(f'<div class="acct"><div class="name">成长中心 · 最近一次 '
                         f'{tk.get("generated") or "—"}</div>{rows}</div>')
        except Exception:
            pass
    blocks["calendar"] = calendar_block(data)
    parts = [blocks[k] for k in ("overview", "history", "dispatch", "calendar")
             if blocks.get(k)] + parts
    return HTML.format(gen=data["generated"] or "—", body="".join(parts) or "<p>没有数据</p>") + LIVE_JS


def main():
    data = build()
    (OUT / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "report.html").write_text(render(data), encoding="utf-8")
    for a in data["accounts"]:
        if a.get("error"):
            log(f"{a['user']}: 失败 {a['error']}")
        else:
            log(f"{a['user']}: 剩 {a['total_left']}，{len(a['packs'])} 批，最早到期 "
                f"{a.get('earliest')} 走 {a.get('earliest_amount')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
