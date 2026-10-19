#!/usr/bin/env python3
"""Turn a scan report into (1) ntfy.sh phone notifications and (2) the private website (plaintext site/index.html,
which the workflow then encrypts with StatiCrypt before publishing).
Usage: python3 publish.py report.json [--no-notify]      |      python3 publish.py --test
Env: NTFY_TOPIC (secret). Prints nothing sensitive (safe for public CI logs).
"""
import json, os, sys, html, urllib.request, datetime

NTFY = 'https://ntfy.sh/'
FAIL_ALERT_AT = 8   # notify once when a source has failed 8 runs in a row (~2 hours)

def ntfy(title, message, click=None, tags=None, priority=3):
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    if not topic: print('NTFY_TOPIC not set; skipping notification'); return False
    body = {'topic': topic, 'title': title, 'message': message, 'priority': priority}
    if click: body['click'] = click
    if tags: body['tags'] = tags
    req = urllib.request.Request(NTFY, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.status == 200
    except Exception as e:
        print('ntfy publish failed:', type(e).__name__); return False

def money(v): return f'${v:,.0f}' if isinstance(v, (int, float)) else '?'
def e(v): return html.escape('' if v is None else str(v))

def titled_txt(l):
    if l.get('titles'): return l['titles']
    return {True: 'Titled', False: 'Untitled', None: 'Title unknown'}[l.get('titled')]

def notify(rep):
    if rep.get('first_run'):
        print(f"first run (no saved state): seeded {sum(len(v) for v in rep['current_matches'].values())} current matches, no notifications sent")
        return
    sent = 0
    for a in rep['alerts']:
        title = f"{a.get('suburb') or a.get('estate')} · Lot {a['lot']} · {money(a.get('price'))}"
        bits = [f"{a['brief']}: {a['reason']}", f"{a['estate']}" + (f" ({a['stage']})" if a.get('stage') else ''),
                ' · '.join(x for x in [f"{a['sqm']} m²" if a.get('sqm') else None, f"{a['frontage']} frontage" if a.get('frontage') else None] if x),
                f"{titled_txt(a)} · {a.get('status') or ''}", a.get('street') or '']
        sent += ntfy(title, '\n'.join(b for b in bits if b), click=a.get('url'), tags=['house'], priority=4)
    for n, s in rep['sources'].items():
        if not s['ok'] and s.get('consecutive_failures') == FAIL_ALERT_AT:
            sent += ntfy(f'Land scanner: {n} failing', f"{n} has failed {FAIL_ALERT_AT} scans in a row. Last error: {s['error'][:200]}",
                         tags=['warning'], priority=2)
    print(f"alerts: {len(rep['alerts'])}, notifications sent: {sent}")

CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#1d2330;line-height:1.4}
header{background:#1f4e79;color:#fff;padding:14px 16px}header h1{margin:0;font-size:1.25rem}header p{margin:4px 0 0;font-size:.9rem;opacity:.9}
main{max-width:980px;margin:0 auto;padding:12px}section{background:#fff;border-radius:10px;padding:12px 14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h2{font-size:1.1rem;margin:0 0 4px}.crit{color:#555;font-size:.88rem;margin:0 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px}
.card{border:1px solid #e1e4ea;border-radius:8px;padding:10px 12px}.card .top{display:flex;justify-content:space-between;gap:8px;font-weight:600}
.price{color:#0a7a3d;white-space:nowrap}.card .sub{font-size:.88rem;color:#444;margin-top:3px}.card a{display:inline-block;margin-top:6px;font-size:.88rem}
.pill{display:inline-block;font-size:.75rem;padding:1px 7px;border-radius:9px;background:#e8eef6;margin:3px 4px 0 0}.pill.t{background:#dff3e6}.pill.u{background:#fdf0d5}
.none{color:#777;font-style:italic}table{border-collapse:collapse;width:100%;font-size:.86rem}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid #eee;vertical-align:top}
.ok{color:#0a7a3d;font-weight:600}.bad{color:#b3261e;font-weight:600}.err{color:#777;font-size:.8rem;word-break:break-word}
pre{background:#f0f2f5;padding:10px;border-radius:6px;overflow-x:auto;font-size:.8rem}code{background:#f0f2f5;padding:0 3px;border-radius:3px}
ol li,ul li{margin:4px 0}small{color:#666}
"""

def crit(b):
    p = [', '.join(b.get('suburbs') or []) or 'any suburb', f"max {money(b.get('max_price'))}" if b.get('max_price', 10**12) < 10**12 else 'any price']
    if b.get('min_sqm') or b.get('max_sqm'):
        p.append(f"{b.get('min_sqm') or 0}–{b.get('max_sqm') or '∞'} m²")
    else: p.append('any size')
    p.append('titled lots only' if b.get('titled_only') else 'titled or untitled')
    return ' · '.join(p)

def card(l):
    t = l.get('titled'); cls = 't' if t is True else ('u' if t is False else '')
    sub = ' · '.join(x for x in [f"{l['sqm']} m²" if l.get('sqm') else None, f"{l['frontage']} frontage" if l.get('frontage') else None, l.get('street')] if x)
    pills = f'<span class="pill {cls}">{e(titled_txt(l))}</span><span class="pill">{e(l.get("status") or "status ?")}</span>'
    if l.get('stage'): pills += f'<span class="pill">{e(l["stage"])}</span>'
    if l.get('release'): pills += f'<span class="pill">{e(l["release"])}</span>'
    return (f'<div class="card"><div class="top"><span>Lot {e(l["lot"])} · {e(l.get("suburb"))}</span><span class="price">{money(l.get("price"))}</span></div>'
            f'<div class="sub">{e(l["estate"])}</div><div class="sub">{e(sub)}</div><div>{pills}</div>'
            f'<a href="{e(l.get("url"))}" target="_blank" rel="noopener">Source: {e(l["source"])} price list ↗</a></div>')

HOWTO = """
<section id="howto"><h2>How to change briefs</h2>
<p>Briefs are kept in a GitHub <b>secret</b> (never in the public code), so only you can see or change them.</p>
<ol><li>Open <a href="https://github.com/__REPO__/settings/secrets/actions">github.com/__REPO__ → Settings → Secrets and variables → Actions</a>.</li>
<li>Click the pencil next to <code>BRIEFS_JSON</code> (“Update”). GitHub never shows the old value, so paste the <b>whole</b> list again with your changes. Keep a copy of your current list somewhere safe (the criteria are also shown at the top of each brief on this page).</li>
<li>Save. The next scan (within ~15 min) uses it. To apply it immediately: <a href="https://github.com/__REPO__/actions/workflows/scan.yml">Actions → Land scan → Run workflow</a>.</li></ol>
<p>Format: a JSON list of briefs. Fields: <code>name</code> (label shown here and in notifications), <code>suburbs</code> (list, case-insensitive exact suburb names),
<code>min_sqm</code>/<code>max_sqm</code> (number or <code>null</code> = no limit), <code>max_price</code> (dollars, no commas), <code>titled_only</code> (<code>true</code>/<code>false</code>), <code>active</code> (<code>false</code> pauses a brief).</p>
<pre>[
  {
    "name": "Brief 1 Hilbert",
    "suburbs": ["Hilbert"],
    "min_sqm": null,
    "max_sqm": null,
    "max_price": 370000,
    "titled_only": false,
    "active": true
  },
  {
    "name": "Brief 2 Baldivis 375+ titled",
    "suburbs": ["Baldivis", "Wellard"],
    "min_sqm": 375,
    "max_sqm": 600,
    "max_price": 420000,
    "titled_only": true,
    "active": true
  }
]</pre>
<p><small>Tip: check the JSON at jsonlint.com before saving. If the JSON is invalid the scan fails and you'll see a red ✗ in the Actions tab (no notifications until fixed).
Suburbs only match when the source states one — Satterley/land4sale/parcelpricelist/Spires/Parkland/Woodlands/WeGotLand estates are covered (see Source health).</small></p></section>
"""

def site(rep, path):
    repo = os.environ.get('GITHUB_REPOSITORY', 'Trevorp82/land-scanner')
    run = datetime.datetime.fromisoformat(rep['run_at'])
    when = run.strftime('%a %d %b %Y, %-I:%M %p') + ' Perth time (AWST)'
    secs = [f'<section><h2>{e(b["name"])} — {len(rep["current_matches"].get(b["name"], []))} match(es)</h2><p class="crit">{e(crit(b))}</p>'
            + (('<div class="cards">' + ''.join(card(l) for l in sorted(rep['current_matches'].get(b['name'], []), key=lambda l: (l.get('price') or 0))) + '</div>')
               if rep['current_matches'].get(b['name']) else '<p class="none">No current matches.</p>') + '</section>'
            for b in rep['briefs']]
    if not rep['briefs']: secs = ['<section><p class="none">No active briefs. See “How to change briefs”.</p></section>']
    rows = ''.join(f'<tr><td>{e(n)}</td><td class="{"ok" if s["ok"] else "bad"}">{"OK" if s["ok"] else "FAILED"}</td><td>{s.get("lots", "–")}</td>'
                   f'<td>{"" if s["ok"] else e("last OK: " + str(s.get("last_ok") or "never").replace("T", " ")) + "<div class=err>" + e(s["error"]) + f" ({s.get("consecutive_failures")} in a row)</div>"}</td></tr>'
                   for n, s in rep['sources'].items())
    notes = ''.join(f'<li>{e(x["source"])}: ' + (f'{len(x["partial_errors"])} sub-page(s) failed (' + e(', '.join(sorted(set(y.split(': ')[0] for y in x['partial_errors']))))[:700] + ')' if 'partial_errors' in x else f'{x.get("skipped_dead_releases")} retired release pages skipped (re-checked daily)') + '</li>' for x in rep.get('notes', []))
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Land scanner</title><style>{CSS}</style></head><body>
<header><h1>Perth land scanner</h1><p>Last scan: {e(when)} · scans every ~15 min · took {rep.get('duration_secs')}s</p></header><main>
{''.join(secs)}
<section><h2>Source health</h2><table><tr><th>Source</th><th>Status</th><th>Lots</th><th></th></tr>{rows}</table>
{('<p><small>Notes:</small></p><ul><small>' + notes + '</small></ul>') if notes else ''}</section>
{HOWTO.replace('__REPO__', e(repo))}
<p><small>Prices/status come straight from developer price lists and can lag the developer's own sales office. Always confirm with the selling agent.</small></p>
</main></body></html>"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    open(path, 'w').write(doc)
    print('site written:', sum(len(v) for v in rep['current_matches'].values()), 'matches across', len(rep['briefs']), 'brief(s)')

if __name__ == '__main__':
    if '--test' in sys.argv:
        ok = ntfy('Land scanner: test notification', 'If you can read this, phone alerts from your GitHub land scanner are working. 🏡',
                  click='https://' + os.environ.get('GITHUB_REPOSITORY_OWNER', 'trevorp82').lower() + '.github.io/land-scanner/', tags=['white_check_mark'])
        print('test notification sent' if ok else 'test notification FAILED'); sys.exit(0 if ok else 1)
    rep = json.load(open(sys.argv[1]))
    site(rep, 'site/index.html')
    if '--no-notify' not in sys.argv: notify(rep)
