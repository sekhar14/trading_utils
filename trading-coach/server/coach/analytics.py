"""Trading-style analytics over closed paper trades, plus a self-contained HTML report."""
import html
import json
from datetime import datetime, timedelta

from . import store

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _stats(trades):
    n = len(trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gross_loss = -sum(losses)
    return {
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1) if n else None,
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "expectancy": round(sum(t["pnl"] for t in trades) / n, 2) if n else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "rules_followed_pct": round(100 * sum(t.get("rules_followed", True) for t in trades) / n) if n else None,
    }


def _group(trades, key):
    groups = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: _stats(v) for k, v in groups.items()}


def _hold_minutes(t):
    return (datetime.fromisoformat(t["closed_at"]) - datetime.fromisoformat(t["opened_at"])).total_seconds() / 60


def _time_bucket(t):
    dt = datetime.fromisoformat(t["opened_at"])
    m = (dt.hour * 60 + dt.minute) // 30 * 30
    return f"{m // 60:02d}:{m % 60:02d}"


def _hold_bucket(t):
    m = _hold_minutes(t)
    return "<5 min" if m < 5 else "5-15 min" if m < 15 else "15-60 min" if m < 60 else ">60 min"


def compute(book, profile, days=30):
    since = (store.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    closed = sorted((t for t in book["trades"] if t["status"] == "closed" and t["opened_at"][:10] >= since),
                    key=lambda t: t["closed_at"])
    if not closed:
        return {"period_days": days, "since": since, "summary": _stats([]), "message": "No closed trades in this period."}

    equity, peak, max_dd, cum = [], 0.0, 0.0, 0.0
    for t in closed:
        cum += t["pnl"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        equity.append({"t": t["closed_at"], "id": t["id"], "pnl": t["pnl"], "cum": round(cum, 2)})

    holds = [_hold_minutes(t) for t in closed]
    active_days = sorted({t["opened_at"][:10] for t in closed})
    summary = _stats(closed)
    summary.update({
        "max_drawdown": round(max_dd, 2),
        "best_trade": max(t["pnl"] for t in closed),
        "worst_trade": min(t["pnl"] for t in closed),
        "avg_hold_min": round(sum(holds) / len(holds), 1),
        "trades_per_day": round(len(closed) / len(active_days), 1),
        "active_days": len(active_days),
    })

    after_loss = []
    for prev, t in zip(closed, closed[1:]):
        gap = (datetime.fromisoformat(t["opened_at"]) - datetime.fromisoformat(prev["closed_at"])).total_seconds() / 60
        if prev["pnl"] < 0 and prev["opened_at"][:10] == t["opened_at"][:10] and gap < 15:
            after_loss.append(t)

    result = {
        "period_days": days,
        "since": since,
        "style_profile": profile.get("style", {}),
        "summary": summary,
        "observed_style": _observed_style(closed, summary),
        "by_setup": _group(closed, lambda t: t.get("setup") or "(untagged)"),
        "by_time_of_day": dict(sorted(_group(closed, _time_bucket).items())),
        "by_weekday": {d: s for d, s in sorted(_group(closed, lambda t: datetime.fromisoformat(t["opened_at"]).weekday()).items())},
        "by_underlying": _group(closed, lambda t: t["underlying"]),
        "by_option_type": _group(closed, lambda t: f"{t['side']} {t['option_type']}"),
        "by_holding_time": _group(closed, _hold_bucket),
        "by_emotion": _group(closed, lambda t: t.get("emotion") or "(not recorded)"),
        "by_exit_reason": _group(closed, lambda t: t.get("exit_reason") or "manual"),
        "rules": {"followed": _stats([t for t in closed if t.get("rules_followed", True)]),
                  "broken": _stats([t for t in closed if not t.get("rules_followed", True)])},
        "within_15_min_of_a_loss": _stats(after_loss),
        "equity_curve": equity,
    }
    result["by_weekday"] = {WEEKDAYS[k]: v for k, v in result["by_weekday"].items()}
    result["insights"] = _insights(result)
    return result


def _observed_style(closed, summary):
    hold = summary["avg_hold_min"]
    kind = "scalper" if hold < 10 else "intraday momentum" if hold < 90 else "swing / positional"
    sides = {}
    for t in closed:
        k = f"{t['side']} {t['option_type']}"
        sides[k] = sides.get(k, 0) + 1
    top_side = max(sides, key=sides.get)
    morning = sum(1 for t in closed if t["opened_at"][11:16] < "11:00")
    return {
        "holding_style": kind,
        "most_traded": f"{top_side} ({round(100 * sides[top_side] / len(closed))}% of trades)",
        "entries_before_11am": f"{round(100 * morning / len(closed))}% of trades",
        "avg_hold": f"{summary['avg_hold_min']:.0f} min",
        "avg_lots": round(sum(t["lots"] for t in closed) / len(closed), 1),
    }


def _insights(r):
    out = []
    setups = {k: v for k, v in r["by_setup"].items() if v["trades"] >= 3}
    if setups:
        best = max(setups, key=lambda k: setups[k]["expectancy"])
        worst = min(setups, key=lambda k: setups[k]["expectancy"])
        out.append(f"Best setup: {best}, {_money(setups[best]['expectancy'])} per trade over {setups[best]['trades']} trades.")
        if worst != best and setups[worst]["expectancy"] < 0:
            out.append(f"Weakest setup: {worst}, {_money(setups[worst]['expectancy'])} per trade. Consider dropping or refining it.")
    f, b = r["rules"]["followed"], r["rules"]["broken"]
    if b["trades"]:
        out.append(f"Rule-following trades made {_money(f['pnl'])}; rule-breaking trades made {_money(b['pnl'])} "
                   f"({b['trades']} of {f['trades'] + b['trades']} trades).")
    a = r["within_15_min_of_a_loss"]
    if a["trades"] >= 2:
        out.append(f"Trades taken within 15 min of a loss: {a['trades']}, win rate {a['win_rate']}%, P&L {_money(a['pnl'])}.")
    slots = {k: v for k, v in r["by_time_of_day"].items() if v["trades"] >= 3}
    if slots:
        w = min(slots, key=lambda k: slots[k]["pnl"])
        if slots[w]["pnl"] < 0:
            out.append(f"Weakest time slot: {w} (30 min), {_money(slots[w]['pnl'])} over {slots[w]['trades']} trades.")
    emo = {k: v for k, v in r["by_emotion"].items() if v["trades"] >= 2 and k != "(not recorded)"}
    if emo:
        w = min(emo, key=lambda k: emo[k]["expectancy"])
        if emo[w]["expectancy"] < 0:
            out.append(f"When you felt '{w}', you averaged {_money(emo[w]['expectancy'])} per trade.")
    untagged = r["by_setup"].get("(untagged)")
    if untagged:
        out.append(f"{untagged['trades']} trades had no setup named. Tag every trade so this report stays honest.")
    return out


# ---------------------------------------------------------------- HTML report

def _money(v):
    return "–" if v is None else f"{'−' if v < 0 else ''}₹{abs(v):,.0f}"


def _bars(title, groups, note=""):
    """Horizontal P&L bars, diverging around zero (blue = profit, red = loss); values in a right-hand column."""
    rows = list(groups.items())
    if not rows:
        return ""
    w, label_w, value_w, row_h = 460, 104, 104, 26
    plot = w - label_w - value_w - 8
    span = max(abs(v["pnl"]) for _, v in rows) or 1
    has_neg = any(v["pnl"] < 0 for _, v in rows)
    has_pos = any(v["pnl"] > 0 for _, v in rows)
    zero = label_w + (plot / 2 if has_neg and has_pos else plot if has_neg else 0)
    scale = (plot / 2 if has_neg and has_pos else plot) / span
    h = row_h * len(rows) + 4
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}">',
             f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="0" y2="{h}" class="axis"/>']
    for i, (k, v) in enumerate(rows):
        y = i * row_h + 4
        length = max(abs(v["pnl"]) * scale, 1)
        x = zero if v["pnl"] >= 0 else zero - length
        cls = "pos" if v["pnl"] >= 0 else "neg"
        tip = (f"{k}: {_money(v['pnl'])} · {v['trades']} trades · win {v['win_rate']}% · "
               f"{_money(v['expectancy'])}/trade")
        parts.append(
            f'<g class="hit" data-tip="{html.escape(tip)}">'
            f'<rect x="0" y="{y - 2}" width="{w}" height="{row_h}" fill="transparent"/>'
            f'<text x="{label_w - 8}" y="{y + 13}" text-anchor="end" class="lbl">{html.escape(str(k))}</text>'
            f'<rect x="{x:.1f}" y="{y + 2}" width="{length:.1f}" height="14" rx="4" class="{cls}"/>'
            f'<text x="{w}" y="{y + 13}" text-anchor="end" class="val">{_money(v["pnl"])}'
            f'<tspan class="muted"> · {v["trades"]}</tspan></text></g>')
    parts.append("</svg>")
    return (f'<section class="card"><h3>{html.escape(title)}</h3>'
            f'{f"<p class=note>{html.escape(note)}</p>" if note else ""}{"".join(parts)}</section>')


def _equity(curve):
    w, h, pad = 1280, 260, 70
    vals = [0] + [p["cum"] for p in curve]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    xs = [pad + i * (w - pad - 10) / (len(vals) - 1) for i in range(len(vals))]
    y = lambda v: 12 + (hi - v) * (h - 40) / rng
    pts = " ".join(f"{x:.1f},{y(v):.1f}" for x, v in zip(xs, vals))
    grid = []
    for v in sorted({lo, 0, hi}):
        grid.append(f'<line x1="{pad}" x2="{w - 10}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="{"axis" if v == 0 else "grid"}"/>'
                    f'<text x="{pad - 6}" y="{y(v) + 4:.1f}" text-anchor="end" class="muted">{_money(v)}</text>')
    dots = []
    for x, p in zip(xs[1:], curve):
        tip = f"{p['id']} · {p['t'][:16].replace('T', ' ')} · trade {_money(p['pnl'])} · total {_money(p['cum'])}"
        dots.append(f'<g class="hit" data-tip="{html.escape(tip)}"><circle cx="{x:.1f}" cy="{y(p["cum"]):.1f}" r="8" fill="transparent"/>'
                    f'<circle cx="{x:.1f}" cy="{y(p["cum"]):.1f}" r="3" class="dot"/></g>')
    return (f'<section class="card wide"><h3>Cumulative P&amp;L</h3><svg viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="Cumulative P&L over trades">{"".join(grid)}<polyline points="{pts}" class="line"/>'
            f'{"".join(dots)}</svg></section>')


def _table(groups):
    head = "<tr><th>Setup</th><th>Trades</th><th>Win %</th><th>P&amp;L</th><th>Per trade</th><th>Avg win</th><th>Avg loss</th><th>Rules kept</th></tr>"
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['trades']}</td><td>{v['win_rate']}</td><td>{_money(v['pnl'])}</td>"
        f"<td>{_money(v['expectancy'])}</td><td>{_money(v['avg_win'])}</td><td>{_money(v['avg_loss'])}</td>"
        f"<td>{v['rules_followed_pct']}%</td></tr>"
        for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["pnl"]))
    return f'<section class="card wide"><h3>Setup scorecard</h3><table>{head}{rows}</table></section>'


CSS = """
:root{--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--pos:#2a78d6;--neg:#e34948;--line:#2a78d6;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--surface:#1a1a19;--page:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--pos:#3987e5;--neg:#e66767;--line:#3987e5;color-scheme:dark}}
:root[data-theme=dark]{--surface:#1a1a19;--page:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--pos:#3987e5;--neg:#e66767;--line:#3987e5;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1360px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:28px 0 10px;color:var(--ink2);font-weight:600}
h3{font-size:13px;margin:0 0 10px;font-weight:600}.sub{color:var(--ink2);margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px}
.tile,.card{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:14px 16px}
.tile .k{color:var(--ink2);font-size:12px}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px}.wide{grid-column:1/-1}
svg{width:100%;height:auto;display:block;overflow:visible}.lbl{fill:var(--ink2);font-size:12.5px}.val{fill:var(--ink);font-size:12.5px;font-variant-numeric:tabular-nums}
.muted{fill:var(--muted);color:var(--muted);font-size:11.5px}.pos{fill:var(--pos)}.neg{fill:var(--neg)}.axis{stroke:var(--axis);stroke-width:1}
.grid{stroke:var(--grid);stroke-width:1}.line{fill:none;stroke:var(--line);stroke-width:2}.dot{fill:var(--line);stroke:var(--surface);stroke-width:1.5}
.note{color:var(--ink2);font-size:12px;margin:-4px 0 8px}ul.ins{margin:0;padding-left:18px}ul.ins li{margin:4px 0}
.profile{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--ink2);font-size:13px}.profile b{color:var(--ink);font-weight:600}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}th{color:var(--ink2);font-weight:600;font-size:12px}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);padding:6px 9px;border-radius:6px;font-size:12px;display:none;max-width:320px;z-index:9}
.hit{cursor:default}.hit:hover rect[class],.hit:hover .dot{opacity:.8}
@media (max-width:520px){.grid2{grid-template-columns:1fr}}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('.hit').forEach(g=>{
 g.addEventListener('mousemove',e=>{tip.textContent=g.dataset.tip;tip.style.display='block';
  tip.style.left=Math.min(e.clientX+12,innerWidth-330)+'px';tip.style.top=(e.clientY+12)+'px'});
 g.addEventListener('mouseleave',()=>tip.style.display='none')});
"""


def render_html(r):
    s = r["summary"]
    style = r.get("style_profile", {})
    obs = r.get("observed_style", {})
    tiles = [("Net P&L", _money(s["pnl"])), ("Trades", s["trades"]), ("Win rate", f"{s['win_rate']}%"),
             ("Per trade", _money(s["expectancy"])), ("Profit factor", s["profit_factor"] or "–"),
             ("Max drawdown", _money(s["max_drawdown"])), ("Avg hold", f"{s['avg_hold_min']:.0f} min"),
             ("Rules kept", f"{s['rules_followed_pct']}%")]
    stated = [(k.replace("_", " ").capitalize(), ", ".join(v) if isinstance(v, list) else v)
              for k, v in style.items() if v]
    profile_html = "".join(f"<span>{html.escape(str(k))}: <b>{html.escape(str(v))}</b></span>" for k, v in stated) \
        or "<span>No style profile saved yet. Tell the coach how you trade.</span>"
    observed_html = "".join(f"<span>{html.escape(k.replace('_', ' ').capitalize())}: <b>{html.escape(str(v))}</b></span>"
                            for k, v in obs.items())
    rules = {"Rules followed": r["rules"]["followed"], "Rules broken": r["rules"]["broken"]}
    body = f"""
<main>
<h1>Trading style report</h1>
<p class="sub">Paper trades · last {r['period_days']} days (since {r['since']}) · {s['active_days']} active days · generated {store.now():%d %b %Y %H:%M} IST</p>
<div class="tiles">{''.join(f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in tiles)}</div>
<h2>Your style</h2>
<div class="grid2">
<section class="card"><h3>What you say</h3><div class="profile">{profile_html}</div></section>
<section class="card"><h3>What your trades show</h3><div class="profile">{observed_html}</div></section>
</div>
<h2>What the numbers say</h2>
<section class="card"><ul class="ins">{''.join(f'<li>{html.escape(i)}</li>' for i in r['insights']) or '<li>Not enough trades yet for patterns.</li>'}</ul></section>
<h2>Performance</h2>
<div class="grid2">
{_equity(r['equity_curve'])}
{_bars('P&L by setup', r['by_setup'], 'Net P&L · number of trades. Hover a bar for win rate and per-trade P&L.')}
{_bars('Discipline: rules followed vs broken', rules)}
{_bars('P&L by entry time (30-min slots)', r['by_time_of_day'])}
{_bars('P&L by how you felt at entry', r['by_emotion'])}
{_bars('P&L by holding time', r['by_holding_time'])}
{_bars('P&L by exit reason', r['by_exit_reason'])}
{_bars('P&L by weekday', r['by_weekday'])}
{_bars('P&L by side', r['by_option_type'])}
{_table(r['by_setup'])}
</div>
</main><div id="tip"></div>"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Trading style report</title><style>{CSS}</style></head><body>{body}<script>{JS}</script></body></html>')


def write_report(book, profile, days=30):
    r = compute(book, profile, days)
    if "message" in r:
        return {"written": False, "message": r["message"]}
    out = store.path("reports") / f"style-report-{store.now():%Y%m%d-%H%M}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render_html(r), encoding="utf-8")
    return {"written": True, "path": str(out), "insights": r["insights"], "summary": r["summary"]}
