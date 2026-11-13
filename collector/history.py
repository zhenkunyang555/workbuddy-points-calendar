#!/usr/bin/env python3
"""每日消耗曲线：换模型前后对比（图文块的数据源）。

一路主数据：magpie 的 usage.jsonl —— 每完成一次请求就追一行，带
providerAccount（哪个账号）和 credits（这次扣了多少分）。实测这条最靠得住，
随请求立刻变；按 offset 增量读，只认新长出来的那一段。
一路备数据：magpie 的 credits-daily.json —— 按余额差记账，只有它轮询余额时才动。
网关没扣分、但账号积分确实在少的日子（客户端自己花掉的），才用这一路补位。

为什么不再只信余额账本：2026-11-02 19:45 之后那份账本卡死了（每个账号都记成
已用 0 / 总额 500，`changed` 永远为假，文件不再落盘），柱图从 11-03 起就全是 0。
现在余额账本只是个补位，缺了也不影响曲线；真有一天"有请求却算出 0 分"，会写进
warn 字段，页面直接亮出来，不会再闷声显示 0。

输出 /app/history.json：每天总量、分账号量、每天用了哪路数据、换模型前后日均、SVG 柱状图。
换模型日 = 2026-10-30（DeepSeek-V4.1-Flash → Kimi-K3.1-Pro，推理拉到 max）。
"""
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(os.environ.get("HISTORY_USAGE", "/cfg/magpie/usage.jsonl"))
BAL = Path(os.environ.get("HISTORY_BALANCE", "/cfg/magpie/credits-daily.json"))
LEDGER = Path(os.environ.get("HISTORY_LEDGER", "/app/usage-daily.json"))
REPORT = Path(os.environ.get("HISTORY_REPORT", "/app/report.json"))
OUT = Path(os.environ.get("HISTORY_OUT", "/app/history.json"))
LOGDIR = Path(os.environ.get("HISTORY_LOGDIR", "/app"))
SWITCH = "2026-10-30"
CST = timezone(timedelta(hours=8))
# 柱图固定看「近 30 天」，每天一根柱子，不随月份 30/31 天变。
# 卡片里所有算出来的数（合计、日均、到期预测）都用同一个基数：30 天。
DAYS = 30
KEEP_DAYS = 40


def log(s):
    try:
        with (LOGDIR / "guard.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(CST):%Y-%m-%d %H:%M:%S}  [history] {s}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- usage.jsonl
def load_ledger():
    """{offset, carry(base64 的未完成末行), days:{日:{账号:分}}, rows:{日:请求数}}"""
    st = {"offset": 0, "carry": "", "days": {}, "rows": {}}
    try:
        raw = json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return st
    if not isinstance(raw, dict):
        return st
    st["offset"] = int(raw.get("offset") or 0)
    st["carry"] = str(raw.get("carry") or "")
    for k, v in (raw.get("days") or {}).items():
        if isinstance(v, dict):
            st["days"][str(k)] = {str(a): float(x) for a, x in v.items()
                                  if isinstance(x, (int, float))}
    for k, v in (raw.get("rows") or {}).items():
        try:
            st["rows"][str(k)] = int(v)
        except Exception:
            pass
    return st


def day_of(t):
    """请求时间 → 本地（+08:00）日期。老版本 magpie 写的是 UTC 的 Z 结尾，不能直接切前 10 位。"""
    s = str(t or "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00")) if s.endswith("Z") \
            else datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST).date().isoformat()
    except Exception:
        return s[:10] if len(s) >= 10 else ""


def scan(st):
    """从上次读到的位置往后读，把新出现的 workbuddy 请求累加进按天、按账号的账。"""
    try:
        size = SRC.stat().st_size
    except FileNotFoundError:
        return 0
    if size < st["offset"]:                # 文件被截断或轮转：从头读，已累计的数不清零
        st["offset"], st["carry"] = 0, ""
    if size == st["offset"]:
        return 0
    start = st["offset"]
    try:
        with SRC.open("rb") as f:
            f.seek(start)
            chunk = f.read()
    except Exception as e:
        log(f"读 usage.jsonl 失败：{e}")
        return 0
    try:
        carry = base64.b64decode(st["carry"] or "")
    except Exception:
        carry = b""
    buf = carry + chunk
    lines = buf.split(b"\n")
    tail = lines.pop()                     # 末行可能还没写完，留到下一轮
    st["offset"] = start + len(chunk) - len(tail)
    st["carry"] = base64.b64encode(tail).decode() if tail else ""
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
        day = day_of(d.get("t"))
        if len(day) != 10:
            continue
        st["rows"][day] = st["rows"].get(day, 0) + 1
        who = d.get("providerAccount") or "?"
        try:
            c = float(d.get("credits") or 0)
        except Exception:
            c = 0.0
        bucket = st["days"].setdefault(day, {})
        bucket[who] = bucket.get(who, 0.0) + c
        added += 1
    for key in ("days", "rows"):
        if len(st[key]) > KEEP_DAYS:
            for k in sorted(st[key])[:-KEEP_DAYS]:
                st[key].pop(k, None)
    return added


def save_ledger(st):
    tmp = LEDGER.with_name(LEDGER.name + ".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LEDGER)
    except Exception as e:
        log(f"写 {LEDGER.name} 失败：{e}")


# ------------------------------------------------------------ credits-daily
def load_balance():
    """magpie 的余额差账本 → {日: {账号: 分}}；读不到就是空账，不影响主数据。"""
    daily = {}
    try:
        raw = json.loads(BAL.read_text(encoding="utf-8"))
    except Exception:
        return daily, None
    stamp = None
    try:
        stamp = datetime.fromtimestamp(BAL.stat().st_mtime, CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    for key, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        user = key.split("|")[1] if "|" in key else key
        days = entry.get("days")
        pairs = []
        if isinstance(days, dict):
            pairs = list(days.items())
        elif isinstance(days, list):
            for d in days:
                if isinstance(d, dict):
                    ds = d.get("date") or d.get("day") or d.get("day_date")
                    v = d.get("credits") if d.get("credits") is not None else d.get("amount")
                    if ds is not None and v is not None:
                        pairs.append((ds, v))
        for ds, v in pairs:
            ds = str(ds)[:10]
            try:
                v = float(v)
            except Exception:
                continue
            rec = daily.setdefault(ds, {})
            rec[user] = rec.get(user, 0.0) + v
    return daily, stamp


def spans(days):
    """['2026-10-24','2026-10-25',...] → '10-24~10-27、11-03'"""
    ds = sorted(days)
    if not ds:
        return ""
    out, run = [], [ds[0]]
    for d in ds[1:]:
        prev = datetime.strptime(run[-1], "%Y-%m-%d")
        if datetime.strptime(d, "%Y-%m-%d") - prev == timedelta(days=1):
            run.append(d)
        else:
            out.append(run)
            run = [d]
    out.append(run)
    return "、".join(f"{r[0][5:]}~{r[-1][5:]}" if len(r) > 1 else r[0][5:] for r in out)


def main():
    st = load_ledger()
    added = scan(st)
    save_ledger(st)
    balance, bal_stamp = load_balance()

    today = datetime.now(CST).date()
    series, from_balance = [], []
    for i in range(DAYS - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        u = st["days"].get(d, {})
        ut = round(sum(u.values()), 2)
        b = balance.get(d, {})
        bt = round(sum(b.values()), 2)
        # 两路都只会少算：逐次记账漏掉客户端直接花的分（那几天网关没走），
        # 余额差漏掉账本停更之后的日子。所以取大的那个。
        use_balance = bt > ut
        if use_balance:
            series.append({"date": d, "total": bt, "by": {k: round(v, 2) for k, v in b.items()},
                           "src": "balance", "requests": st["rows"].get(d, 0)})
            from_balance.append(d)
        elif ut > 0:
            series.append({"date": d, "total": ut, "by": {k: round(v, 2) for k, v in u.items()},
                           "src": "usage", "requests": st["rows"].get(d, 0)})
        else:
            series.append({"date": d, "total": 0.0, "by": {}, "src": "none",
                           "requests": st["rows"].get(d, 0)})

    # 自检：有请求却没算出分 = 数据断了，必须让页面看见
    warn = []
    for rec in series[-3:]:
        if rec["requests"] > 0 and rec["total"] <= 0:
            warn.append(f'{rec["date"][5:]} 有 {rec["requests"]} 次请求却没记到积分')
    if not st["days"] and not balance:
        warn.append("两路数据都读不到（usage.jsonl 与 credits-daily.json）")

    # 统计口径固定 30 天：日均一律 = 近 30 天合计 ÷ 30，不管这个月 30 天还是 31 天，
    # 也不管窗口里有没有「没留底」的空白天 —— 基数同一个，数才跟昨天可比。
    def has_data(rec):
        return rec["total"] > 0 or rec["requests"] > 0

    window_total = sum(d["total"] for d in series)
    avg_a = window_total / DAYS
    data_days = sum(1 for d in series if has_data(d))
    blank_days = [d["date"] for d in series if not has_data(d)]
    peak_rec = max(series, key=lambda d: d["total"])

    n = max(len(series), 1)
    W, H = 1080, 244
    pad_l, pad_r = 14, 14
    slot = (W - pad_l - pad_r) / n             # 每根柱子占的横向宽度
    bw = slot * 0.60                            # 柱子本身（剩下的是间距）
    gap = slot - bw
    base = H - 34                               # 柱子底线（下面留给日期）
    ceil = 34                                   # 最高那根的天花板（上面留给数字）
    peak = max([d["total"] for d in series] + [1.0])
    bars, labels = [], []
    labels.append(f'<line x1="{pad_l-6}" y1="{base}" x2="{W-pad_r+6}" y2="{base}" '
                  f'stroke="#e5e7eb" stroke-width="1"/>')
    for i, d in enumerate(series):
        x = pad_l + i * slot + gap / 2
        if d["total"] > 0:
            h = max(4.0, (base - ceil) * (d["total"] / peak))
        else:
            h = 2.5                             # 那天没记录：留一条浅灰底座，别当成「花了 0 分」
        color = "#2563eb" if d["date"] >= SWITCH else "#9ca3af"
        if d["total"] <= 0:
            color = "#e8e8e8"
        elif d["src"] == "balance":
            color = "#f59e0b"      # 余额差补位的日子，橙色，和主数据区分开
        detail = "　".join(f"{k} {v:.0f}" for k, v in
                           sorted(d["by"].items(), key=lambda x: -x[1])[:4])
        today_tag = "（今天，还没过完）" if d["date"] == today.isoformat() else ""
        tip = (f'{d["date"]}{today_tag} 共 {d["total"]:.2f} 分' +
               (f'（{detail}）' if detail else '') +
               ("；余额差补位" if d["src"] == "balance" else "") +
               (f'；{d["requests"]} 次请求' if d["requests"] else ''))
        bars.append(f'<g><title>{tip}</title>'
                    f'<rect x="{x:.1f}" y="{base-h:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                    f'rx="{min(4, bw/4):.1f}" fill="{color}"/></g>')
        if d["total"] > 0:
            ly, lc = base - h - 5, "#374151"
            if ly < 12:                  # 柱子顶到天花板了：数字写进柱子里，别被裁掉
                ly, lc = base - h + 16, "#ffffff"
            bars.append(f'<text x="{x+bw/2:.0f}" y="{ly:.1f}" font-size="11" '
                        f'text-anchor="middle" fill="{lc}">{d["total"]:.0f}</text>')
        # 31 根柱子挤在一起，日期不写满：每 5 天标一个，再补第一根和今天
        dom = int(d["date"][8:10]) if len(d["date"]) >= 10 else 0
        if dom % 5 == 0 or i == 0 or d["date"] == today.isoformat():
            labels.append(f'<line x1="{x+bw/2:.1f}" y1="{base}" x2="{x+bw/2:.1f}" '
                          f'y2="{base+4}" stroke="#d1d5db"/>')
            labels.append(f'<text x="{x+bw/2:.0f}" y="{H-10}" font-size="11" '
                          f'text-anchor="middle" fill="#6b7280">{d["date"][5:]}</text>')
    try:
        idx = [d["date"] for d in series].index(SWITCH)
        xline = pad_l + idx * slot + slot / 2
        bars.append(f'<line x1="{xline:.1f}" y1="12" x2="{xline:.1f}" y2="{base}" '
                    f'stroke="#b45309" stroke-dasharray="4 3"/>')
        # 标注放右上/左上角，别压在最高那根柱子的数字上
        if idx < n * 0.7:
            bars.append(f'<text x="{W-8}" y="18" font-size="12" fill="#b45309" text-anchor="end">10-30 换 Kimi（当晚又换回 flash）</text>')
        else:
            bars.append(f'<text x="8" y="18" font-size="12" fill="#b45309">10-30 换 Kimi（当晚又换回 flash）</text>')
    except ValueError:
        pass
    svg = (f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto" '
           f'style="max-width:100%;display:block;margin:2px 0 6px">'
           + "".join(bars) + "".join(labels) + "</svg>")

    # 11 月到期 vs 还能花掉多少：基数同样是 30 天日均
    nov_total, days_left = 0.0, 0
    try:
        rep = json.loads(REPORT.read_text(encoding="utf-8"))
        for a in rep.get("accounts") or []:
            for p in a.get("packs") or []:
                if p.get("end_local", "") < "2026-12-01":
                    nov_total += p.get("left", 0)
        last = datetime(2026, 11, 30, 23, 59, tzinfo=CST)
        days_left = max(0, (last - datetime.now(CST)).days)
    except Exception:
        pass
    can_spend = avg_a * days_left
    waste = max(0.0, nov_total - can_spend)
    if days_left <= 0:
        forecast = f"11 月到期 {nov_total:.0f} 分，本月已经过完，没花掉的就是作废"
    else:
        forecast = (f"11 月到期 {nov_total:.0f} 分，按近 30 天日均 {avg_a:.0f} 分（合计÷30）、"
                    f"还剩 {days_left} 天，最多能花 {can_spend:.0f} 分"
                    + (f"，预计作废约 {waste:.0f} 分" if waste > 1 else "，正好能花完"))

    note = "数据源：网关逐次请求扣分（magpie usage.jsonl，增量读、每轮只扫新长的部分）"
    if bal_stamp:
        note += f"＋余额差（credits-daily.json，只到 {bal_stamp}）"
    note += "。每天取两路里更大的那个：逐次记账漏掉客户端直接花的分，余额差漏掉账本停更之后的日子，两边都只会少算"
    if from_balance:
        note += f"；现在靠余额差补的是 {spans(from_balance)}（橙色柱子）"
    note += (f"。这张图从左到右是近 {n} 天的逐日消耗：合计 {window_total:.0f} 分，"
             f"日均 {avg_a:.1f} 分（一律用 30 天做基数，合计 ÷ 30）")
    if blank_days:
        note += f"；{spans(blank_days)} 那几天没有留底数据，柱子上是空的"
    note += "。"

    out = {
        "generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "switch": SWITCH,
        "series": series,
        "days": n,
        "start": series[0]["date"] if series else "",
        "end": series[-1]["date"] if series else "",
        "data_days": data_days,
        "avg": round(avg_a, 2),
        "window_total": round(window_total, 2),
        "peak_date": peak_rec["date"],
        "peak_total": round(peak_rec["total"], 2),
        "forecast": forecast,
        "note": note,
        "legend": (f"每根柱子 = 一天（近 {n} 天；日均 = 合计 ÷ 30）　灰色 = 10-30 之前　蓝色 = 10-30 及之后（10-30 换 Kimi 约两小时，深夜换回 flash）"
                   + ("　橙色 = 这天余额差比逐次记账多（客户端直接花的），取余额差" if from_balance
                      else "")),
        "warn": "；".join(warn),
        "source": "usage.jsonl（逐次请求）＋ credits-daily.json（缺数补位）",
        "summary": (
            f"近 {n} 天共 {window_total:.0f} 分，日均 {avg_a:.1f} 分（合计 ÷ 30）"
            + f"；最高一天 {peak_rec['total']:.0f} 分（{peak_rec['date'][5:]}）"
            + (f"；另有 {len(blank_days)} 天没留底" if blank_days else "")),
        "svg": svg,
    }
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OUT)
    log(f"日均对比：{out['summary']}；新读 {added} 条请求" + (f"；警告：{out['warn']}" if warn else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
