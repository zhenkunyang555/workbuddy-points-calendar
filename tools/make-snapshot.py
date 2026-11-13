#!/usr/bin/env python3
"""从采集端产出的 report.json / report.html 生成静态快照（三份一起刷）。

用法：
    python3 tools/make-snapshot.py --report report.json --html report.html
    python3 tools/make-snapshot.py --report report.json          # 只刷 data.json + calendar.html
    python3 tools/make-snapshot.py --report report.json --no-aliases   # 保留真实账号名（只给自己看时用）

默认会读 `tools/aliases.json`，把真实账号名换成「账号 1 / 账号 2 …」——仓库是公开的，
快照里不该出现真名。别名文件长这样（键是 report.json 里的 user，值是页面上显示的名字）：

    {"张三": "账号 1", "li.si": "账号 2"}

口径：
- 只统计 `counted: true` 的批次（report.py 已把不计入的标出来），按 `end_local` 的日期归集
- 窗口从「今天」起算 `--days` 天（默认 45，与报告页「45 天内到期」一致），中间没有到期的日子补 0，
  这样柱状图的时间轴是连续的
- 每行：{"d": 日期, "tot": 当天合计, "<账号>": 当天该账号到期分}
- 同时把 calendar.html 的 `const rows=[...]` 和纵轴刻度一起刷新；给了 --html 就把报告页也刷成
  index.html（去掉轮询 today.json 的 <script>、补 viewport、换掉真名）

无第三方依赖，Python 3.9+。
"""
import argparse
import collections
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def tidy_label(name):
    """别名里允许写「账号1」，页面上统一成「账号 1」（中英混排时留个空格更好读）。"""
    m = re.fullmatch(r"(账号|account|acct)\s*(\d+)", str(name).strip(), re.I)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return str(name).strip()


def load_aliases(path, enabled=True):
    if not enabled or not path:
        return {}
    p = pathlib.Path(path)
    if not p.is_file():
        return {}
    return {str(k): tidy_label(v) for k, v in load_json(p).items()}


def display_name(user, aliases):
    return aliases.get(str(user), str(user))


def build_rows(report, days, aliases):
    accounts = [a.get("user") or "?" for a in report.get("accounts") or []]
    labels = [display_name(u, aliases) for u in accounts]
    per_day = collections.defaultdict(lambda: collections.defaultdict(float))
    for acc in report.get("accounts") or []:
        label = display_name(acc.get("user") or "?", aliases)
        for pack in acc.get("packs") or []:
            if not pack.get("counted"):
                continue
            day = (pack.get("end_local") or "")[:10]
            if not day:
                continue
            per_day[day][label] += float(pack.get("left") or 0)

    today = dt.date.today()
    rows = []
    for i in range(days):
        d = (today + dt.timedelta(days=i)).isoformat()
        row: dict[str, object] = {"d": d}
        total = 0.0
        for label in labels:
            v = round(per_day.get(d, {}).get(label, 0.0), 2)
            row[label] = v
            total += v
        row["tot"] = round(total, 2)
        rows.append(row)
    # 键顺序：d、tot、账号……（与历史快照一致，读起来稳定）
    return [{k: r[k] for k in ["d", "tot", *labels]} for r in rows]


def axis_ticks(max_total):
    if max_total <= 0:
        return [100, 200, 300]
    step = 10 ** max(0, len(str(int(max_total))) - 1)
    for cand in (step, step * 2, step * 5, step * 10):
        if max_total / cand <= 4:
            return [cand, cand * 2, cand * 3]
    step *= 10
    return [step, step * 2, step * 3]


def write_calendar(rows, path, generated="", accounts=None, beyond=None):
    html = path.read_text(encoding="utf-8")
    labels = [k for k in rows[0] if k not in ("d", "tot")] if rows else []
    payload = json.dumps(rows, ensure_ascii=False)
    html, n = re.subn(r"const rows=\[.*?\];", lambda _m: "const rows=" + payload + ";", html, count=1, flags=re.S)
    if not n:
        sys.exit("calendar.html 里没找到 const rows=[...]；文件结构变了，先修本脚本的匹配式")
    ticks = axis_ticks(max((r["tot"] for r in rows), default=0))
    html, n2 = re.subn(r"\[[0-9,\s]+\]\.forEach\(v=>", lambda _m: "[" + ",".join(str(t) for t in ticks) + "].forEach(v=>",
                       html, count=1)
    if not n2:
        sys.exit("calendar.html 里没找到纵轴刻度那行；文件结构变了，先修本脚本的匹配式")

    # 图例：账号数组与配色。配色按固定顺序循环，加账号也不会串色
    palette = ["#8b8f98", "#4f8ef7", "#2fb37a", "#e0613a", "#a86ee8", "#e0a13a"]
    users_js = "const users=" + json.dumps(labels, ensure_ascii=False) + ";"
    col_js = "const col={" + ",".join(
        json.dumps(k, ensure_ascii=False) + ":" + json.dumps(palette[i % len(palette)]) for i, k in enumerate(labels)
    ) + "};"
    html, n3 = re.subn(r"const users=\[.*?\];", lambda _m: users_js, html, count=1)
    html, n4 = re.subn(r"const col=\{.*?\};", lambda _m: col_js, html, count=1)
    if not (n3 and n4):
        sys.exit("calendar.html 里没找到 const users/col；文件结构变了，先修本脚本的匹配式")

    # 标题旁的生成时间与账号数
    if generated:
        html = re.sub(r"数据：magpie-guard report\.json · 生成 [0-9: -]+",
                      lambda _m: f"数据：采集端 report.json · 生成 {generated}", html, count=1)
    if accounts is not None:
        html = re.sub(r"每天到期的积分（\d+ 个号合计）",
                      lambda _m: f"每天到期的积分（{accounts} 个号合计）", html, count=1)

    # 正文说明：原来是人写死的（「前面 25 天全是 0…」），现在按数据重算
    nz = [r for r in rows if r["tot"] > 0]
    if nz:
        peak = max(nz, key=lambda r: r["tot"])
        peak_md = f"{int(peak['d'][5:7])}/{int(peak['d'][8:10])}"
        first_md = f"{int(nz[0]['d'][5:7])}/{int(nz[0]['d'][8:10])}"
        last_md = f"{int(nz[-1]['d'][5:7])}/{int(nz[-1]['d'][8:10])}"
        parts = [f"图上是 {rows[0]['d']} 起 {len(rows)} 天；{first_md} 开始有到期，"
                 f"<span class=\"big\">{peak_md} 一天掉 {peak['tot']:,.0f} 分</span>，最后一根柱子是 {last_md}。"]
        if beyond:
            parts.append(f"{beyond} 之后的到期批次不在图上，见首页「到期日程」。")
        note = "<br>".join(parts)
    else:
        note = f"{rows[0]['d']} 起 {len(rows)} 天内没有要过期的批次。"
    html = re.sub(r'<div class="note">.*?</div>', lambda _m: f'<div class="note">{note}</div>', html, count=1, flags=re.S)

    path.write_text(html, encoding="utf-8")
    return ticks


def write_index(report_html, out_path, aliases):
    """报告页 → 静态快照：去掉轮询 today.json 的脚本、补 viewport、换掉真实账号名。"""
    html = pathlib.Path(report_html).read_text(encoding="utf-8")
    html = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    if 'name="viewport"' not in html:
        html = html.replace('<meta charset="utf-8">',
                            '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">', 1)
    # 长名字先换，避免替换结果又被别的规则命中
    for real in sorted(aliases, key=len, reverse=True):
        html = html.replace(real, aliases[real])
    leftovers = sorted(n for n in aliases if n in html)
    if leftovers:
        sys.exit("别名替换不干净，仍能看到：" + "、".join(leftovers))
    pathlib.Path(out_path).write_text(html, encoding="utf-8")
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(ROOT / "report.json"))
    ap.add_argument("--html", default=str(ROOT / "report.html"),
                    help="采集端的 report.html；存在就一起刷 index.html")
    ap.add_argument("--index", default=str(ROOT / "index.html"))
    ap.add_argument("--data", default=str(ROOT / "data.json"))
    ap.add_argument("--calendar", default=str(ROOT / "calendar.html"))
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--aliases", default=str(ROOT / "tools" / "aliases.json"))
    ap.add_argument("--no-aliases", action="store_true", help="保留真实账号名（只给自己看时用）")
    args = ap.parse_args()

    aliases = load_aliases(args.aliases, enabled=not args.no_aliases)
    report = load_json(args.report)
    rows = build_rows(report, args.days, aliases)
    pathlib.Path(args.data).write_text(json.dumps(rows, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")

    # 窗口之外还有没有到期批次（calendar 页脚要说一声）
    horizon = rows[-1]["d"] if rows else ""
    later = sorted({
        (pack.get("end_local") or "")[:10]
        for acc in report.get("accounts") or []
        for pack in acc.get("packs") or []
        if pack.get("counted") and (pack.get("end_local") or "")[:10] > horizon
    })
    ticks = write_calendar(rows, pathlib.Path(args.calendar),
                           generated=str(report.get("generated") or ""),
                           accounts=len(report.get("accounts") or []),
                           beyond=later[0] if later else None)
    wrote_index = pathlib.Path(args.html).is_file()
    if wrote_index:
        write_index(args.html, args.index, aliases)

    labels = [display_name(a.get("user") or "?", aliases) for a in report.get("accounts") or []]
    nonzero = [r for r in rows if r["tot"] > 0]
    peak = max(nonzero, key=lambda r: r["tot"]) if nonzero else None
    print(f"账号（{'已脱敏' if aliases else '原名'}）：{'、'.join(labels) or '（空）'}")
    print(f"快照：{len(rows)} 天（{rows[0]['d']} → {rows[-1]['d']}），有到期的 {len(nonzero)} 天")
    if peak:
        print(f"峰值：{peak['d']} 合计 {peak['tot']}（纵轴刻度 {ticks}）")
    print("写入 " + " / ".join([args.data, args.calendar] + ([args.index] if wrote_index else [])))


if __name__ == "__main__":
    main()
