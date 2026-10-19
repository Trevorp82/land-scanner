#!/usr/bin/env python3
"""land-watch scanner: fetch developer price lists, normalise lots, match briefs, alert on new matches.
Usage: python3 scan.py [--all] [--report FILE] [--quiet]
  --all          also include every current match (not just new/changed ones)
  --report FILE  write the full JSON report to FILE instead of stdout
  --quiet        print only a non-sensitive per-source health summary to stdout (safe for public CI logs)
Env:
  BRIEFS_JSON    briefs as a JSON array (overrides briefs.json) - used in GitHub Actions via a secret
  STATE_DIR      directory holding seen.json and cache/ (default: script dir)
State in seen.json, fetch cache in cache/ (PDFs are cached as extracted text, so the cache stays small).
"""
import re, html, json, os, sys, time, subprocess, hashlib, datetime, traceback, urllib.request, urllib.error
from zoneinfo import ZoneInfo

D = os.path.dirname(os.path.abspath(__file__))
SD = os.environ.get('STATE_DIR') or D
CACHE = os.path.join(SD, 'cache'); os.makedirs(CACHE, exist_ok=True)
SEEN = os.path.join(SD, 'seen.json'); BRIEFS = os.path.join(D, 'briefs.json')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36 land-watch/1.0'
TIMEOUT = 60
PERTH = ZoneInfo('Australia/Perth')
NOW_DT = datetime.datetime.now(PERTH).replace(tzinfo=None)
NOW = NOW_DT.isoformat(timespec='seconds')   # Perth local time, regardless of the machine's timezone
STATE = {}  # the loaded seen.json, available to sources that need memory (e.g. Spires dead releases)
LOG = []   # per-request notes

def fetch(url, binary=False, cond=True, pdf=False, timeout=None):
    """GET with timeout + polite delay + ETag/Last-Modified caching. Returns bytes or str.
    pdf=True: returns the pdftotext text and caches only that text (not the multi-MB PDF)."""
    h = hashlib.md5(url.encode()).hexdigest()
    body_p, meta_p = os.path.join(CACHE, h + ('.txt' if pdf else '')), os.path.join(CACHE, h + '.json')
    if pdf: binary = True
    meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    if cond and os.path.exists(body_p):
        if meta.get('etag'): req.add_header('If-None-Match', meta['etag'])
        if meta.get('lm'): req.add_header('If-Modified-Since', meta['lm'])
    to = timeout or TIMEOUT
    time.sleep(0.5)
    try:
        with urllib.request.urlopen(req, timeout=to) as r:
            data = r.read()
            if pdf: data = pdf_text(data).encode()
            if cond:
                json.dump({'etag': r.headers.get('ETag'), 'lm': r.headers.get('Last-Modified'), 'url': url, 'at': NOW}, open(meta_p, 'w'))
                open(body_p, 'wb').write(data)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            data = open(body_p, 'rb').read()
            return data.decode('utf-8', 'ignore') if pdf else (data if binary else data.decode('utf-8', 'ignore'))
        elif e.code in (403, 406, 500, 503):
            # some hosts' mod_security rejects urllib's TLS/header fingerprint; retry same public URL via curl
            time.sleep(2)
            p = subprocess.run(['curl', '-sfL', '-m', str(to), '-A', UA, url], capture_output=True, timeout=to + 10)
            if p.returncode != 0: raise
            data = p.stdout
            if pdf: data = pdf_text(data).encode()
        else:
            raise
    if pdf: return data.decode('utf-8', 'ignore')
    return data if binary else data.decode('utf-8', 'ignore')

def pdf_text(data):
    p = subprocess.run(['pdftotext', '-layout', '-', '-'], input=data, capture_output=True, timeout=120)
    return p.stdout.decode('utf-8', 'ignore')

def num(s):
    if s is None: return None
    m = re.search(r'\d[\d,]*\.?\d*', str(s))
    if not m: return None
    v = float(m.group(0).replace(',', ''))
    return int(v) if v.is_integer() else v

def lines_of_html(t):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', '', t, flags=re.S)
    return [l.strip() for l in html.unescape(re.sub(r'<[^>]+>', '\n', t)).split('\n') if l.strip()]

def table_rows(t):
    out = []
    for tr in re.findall(r'<tr.*?</tr>', t, re.S):
        c = [' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', x)).split()) for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        if c: out.append(c)
    return out

def lot(source, estate, suburb, lotno, street, sqm, frontage, price, titled, status, url, **extra):
    return dict(source=source, estate=estate, suburb=suburb, lot=str(lotno).strip(), street=(street or '').strip() or None,
                sqm=num(sqm), frontage=(str(frontage).strip() if frontage not in (None, '') else None),
                price=num(price) if price and '$' in str(price) else (num(price) if isinstance(price, (int, float)) else None),
                titled=titled, status=status, url=url, **extra)

# ---------------- sources ----------------
PARCEL_SUBURBS = {  # estate name prefix -> suburb (only ones confirmed in our searches)
    'Claravale': 'Hilbert', 'Brightwood': 'Baldivis', 'Beenyup Grove': 'Byford', 'Indi': 'Yanchep',
    'Mosaic': 'Balcatta', 'Kennedy Bay': 'Port Kennedy',
}
def src_parcel():
    url = 'https://parcelpricelist.com.au/'
    raw = fetch(url, cond=False)
    if 'Lot ' not in raw: raise RuntimeError('page has no lots / layout changed')
    pdfs = dict(re.findall(r'price-list-pdfs/([a-z0-9\-]+)\.pdf\?v=(\d+)', raw))
    L = lines_of_html(raw); head = re.compile(r'^(.+?)\s{2,}(Stage .+)$')
    out, est, stage, i = [], None, None, 0
    while i < len(L):
        m = head.match(L[i])
        if m: est, stage = m.group(1).strip(), m.group(2).strip()
        elif est and re.match(r'^Lot \d+', L[i]):
            f = L[i+1:i+17]
            try:
                slug = est.lower().replace(' ', '-')
                titles = f[12]
                sub = next((v for k, v in PARCEL_SUBURBS.items() if est.startswith(k)), None)
                out.append(lot('parcelpricelist', est, sub, re.sub(r'^Lot\s*', '', L[i]), f[0], f[5], f[1] + 'm', f[13],
                               titles.strip().lower() == 'titled', f[8],
                               f'https://parcelpricelist.com.au/assets/price-list-pdfs/{slug}.pdf?v={pdfs.get(slug, "")}',
                               stage=stage, titles=titles))
            except IndexError: pass
        elif 'Disclaimer' in L[i] or L[i] == 'Close': est = None
        i += 1
    return out

def satterley_estates():
    t = fetch('https://satterley.com.au/land-for-sale/wa/', cond=False)
    L = lines_of_html(t); est = {}
    # cards look like: "<Estate>, " / "<Suburb>" / "Now Selling" ...
    for i, x in enumerate(L):
        m = re.match(r'^(.+?),\s*$', x)
        if m and i + 1 < len(L):
            name, sub = m.group(1).strip(), L[i+1].strip()
            if 1 < len(name) < 40 and 1 < len(sub) < 40 and not any(c.isdigit() for c in name + sub):
                est.setdefault(name, sub)
    slugs = set(re.findall(r'https://satterley\.com\.au/([a-z0-9-]+)/?["\']', t))
    res = {}
    for name, sub in est.items():
        cand = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        for s in (cand, cand + '-estate'):
            if s in slugs: res[s] = (name, sub); break
    return res

def parse_satterley_pdf(txt, name, sub, url):
    out, stage, titled = [], None, None
    for l in txt.splitlines():
        if re.match(r'^\s{0,3}Stage\s+\S+', l):
            stage = ' '.join(l.split()); rest = stage
            titled = bool(re.search(r'\bTitled\b', rest)) and not re.search(r'(?i)untitled', rest)
            continue
        if 'Title Forecast' in l: stage, titled = None, None
        m = re.match(r'\s*(\d{2,5})\s{2,}(.+?)\s{2,}(\d{2,4})\s+(\S+)\s.*?(\$[\d,]+)\s*(.*)$', l)
        if m and stage:
            out.append(lot('satterley', name, sub, m.group(1), m.group(2), m.group(3), m.group(4) + 'm', m.group(5), titled,
                           'Listed (price list shows unsold lots)', url, stage=stage, notes=m.group(6).strip() or None))
    return out

def src_satterley():
    out, errs = [], []
    for slug, (name, sub) in sorted(satterley_estates().items()):
        url = f'https://satterley.com.au/wp-content/uploads/pricelists/{slug}-pricelist.pdf'
        try:
            try:
                txt = fetch(url, pdf=True)
            except urllib.error.HTTPError as e:
                if e.code != 404 or not slug.endswith('-estate'): raise
                url = url.replace(f'{slug}-pricelist', f'{slug[:-7]}-pricelist'); txt = fetch(url, pdf=True)
            out += parse_satterley_pdf(txt, name, sub, url)
        except Exception as e:
            errs.append(f'{slug}: {e}')
    if errs: LOG.append({'source': 'satterley', 'partial_errors': errs})
    return out

def src_spires():
    sm = fetch('https://www.spiresbaldivis.com.au/release-sitemap.xml', cond=False)
    out = []
    errs = []; skipped = 0
    # old/retired release pages return HTTP 500 every time; remember them and only re-check each one once a day
    dead = STATE.setdefault('spires_dead_releases', {})
    for u in sorted(set(re.findall(r'https://www\.spiresbaldivis\.com\.au/release/[a-z0-9-]+/', sm))):
        relname = u.rstrip('/').split('/')[-1]
        last = dead.get(relname)
        if last and (NOW_DT - datetime.datetime.fromisoformat(last)).total_seconds() < 86400:
            skipped += 1; continue
        try:
            t = fetch(u, cond=False, timeout=30)
            dead.pop(relname, None)
        except Exception as e:
            dead[relname] = NOW
            errs.append(f"{relname}: {e}"); continue
        front = {}
        for c in table_rows(t):
            if c and c[0].startswith('Lot') and len(c) >= 4: front[c[0].split()[-1]] = c[3] if c[3] != '-' else None
        rel = u.rstrip('/').split('/')[-1]
        for typ, fn, href in re.findall(r'data-type="([^"]+)"[^>]*data-firstname="([^"]*)"[^>]*href="([^"]+)"', t):
            s = ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', html.unescape(fn))).split())
            m = re.search(r'Lot\s+(\d+).*?Price:\s*(\$[\d,]+).*?Lot Size:\s*(\d+)', s)
            if not m: continue
            st = 'Available' if typ.startswith('available') else typ
            out.append(lot('spires', 'Spires Private Estate', 'Baldivis', m.group(1), None, m.group(3), front.get(m.group(1)),
                           m.group(2), None, st, href, release=rel))
    if errs: LOG.append({'source': 'spires', 'partial_errors': errs})
    if skipped: LOG.append({'source': 'spires', 'skipped_dead_releases': skipped})
    if not out and errs: raise RuntimeError('all Spires release pages failed')
    return out

def drive_pdf_from(page_url):
    t = fetch(page_url, cond=False)
    m = re.search(r'href="https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/[^"]*"[^>]*>(?:<[^>]+>|\s)*[^<]{0,30}PRICE LIST', t, re.I)
    if not m: raise RuntimeError('price list link not found on ' + page_url)
    fid = m.group(1)
    return fid, fetch(f'https://drive.google.com/uc?export=download&id={fid}', pdf=True)

def src_rppl(name, suburb, page):
    fid, data = drive_pdf_from(page)
    txt = data; view = f'https://drive.google.com/file/d/{fid}/view'
    out, stage = [], None
    for l in txt.splitlines():
        if re.search(r'(?i)^\s*(The .* Release - )?Stage \w+\s*$', l): stage = l.strip(); continue
        # Parkland: lot street frontage size zoning titles price status ; Woodlands: lot street frontage area bal zoning titles price status
        m = re.match(r"\s*(\d{2,4})\s+(.+?)\s{2,}(Cnr|[\d.]+)\s+(\d{3,4})\s+(?:(\S+)\s+)?(RMD\d+|R\d+)\s+(Titled|[A-Za-z]{3}\s*\S+)\s+(\$[\d,]+)\s+(\w+)", l)
        if m:
            titles = m.group(7)
            out.append(lot(name.lower().replace(' ', '-'), name, suburb, m.group(1), m.group(2), m.group(4),
                           None if m.group(3) == 'Cnr' else m.group(3) + 'm', m.group(8), titles == 'Titled', m.group(9), view,
                           stage=stage, titles=titles))
    if not out: raise RuntimeError('no lots parsed from PDF')
    return out

def src_parkland(): return src_rppl('Parkland Heights', 'Baldivis', 'https://www.parklandheights.com.au/building-resources')
def src_woodlands(): return src_rppl('Woodlands Baldivis', 'Baldivis', 'https://www.woodlandsbaldivis.com.au/building-resources')

def src_land4sale():
    idx = fetch('https://land4sale.com.au/our-land/', cond=False)
    out, errs = [], []
    for slug in sorted(set(re.findall(r'land4sale\.com\.au/our-land/([a-z0-9-]+)/', idx))):
        url = f'https://land4sale.com.au/our-land/{slug}/'
        try:
            t = fetch(url, cond=False)
            tm = re.search(r'<title>([^<]*)', t); title = html.unescape(tm.group(1)) if tm else ''
            sm = (re.search(r'In ([A-Z][A-Za-z ]+?) WA', title)
                  or re.search(r'([A-Z][A-Za-z]+(?: [A-Z][a-z]+)?), WA, 6\d{3}', html.unescape(re.sub(r'<[^>]+>', ' ', t))))
            sub = sm.group(1).strip() if sm else None
            est = re.sub(r'(?i)^land for sale\s*|\s*[|-]\s*land ?4 ?sale.*$', '', title.split(' In ')[0]).strip()
            segs = [x.strip() for x in est.split('|') if x.strip() and ', WA' not in x and not re.search(r'(?i)selling|land ?4 ?sale', x)]
            est = segs[0] if segs else est
            est = re.sub(r'(?i)\s*(private estate)?\s*at \w+$', '', est).strip() or slug
            rows = table_rows(t); hdr = None
            plain = ' '.join(lines_of_html(t))
            for c in rows:
                low = [x.lower() for x in c]
                if 'lot' in low[0] and any('price' in x for x in low): hdr = low; continue
                if not hdr or len(c) != len(hdr): continue
                g = lambda key: next((c[i] for i, h in enumerate(hdr) if key in h), None)
                pr = g('price') or ''; lt = g('lot') or ''
                if re.search(r'(?i)sold', pr): st = 'Sold'
                elif re.search(r'\bR\b', lt): st = 'Reserved'
                elif re.search(r'(?i)under offer|hold', pr): st = pr
                else: st = 'Available'
                tit = g('title')
                out.append(lot('land4sale', est, sub, re.sub(r'[^\d]', '', lt) or lt, g('street'), g('sqm') or g('area') or g('size'),
                               g('front'), pr, (tit.lower().startswith('titled') if tit else None), st, url))
        except Exception as e:
            errs.append(f'{slug}: {e}')
    if errs: LOG.append({'source': 'land4sale', 'partial_errors': errs})
    return out

def src_wegotland():
    url = 'https://wegotland.com.au/documents/WeGotLand_TreebyPark_Anketell_Pricelist.pdf'
    txt = fetch(url, pdf=True); out = []
    for l in txt.splitlines():
        m = re.match(r'\s*(\S+)\s+(\d{1,4})\s+(.+?)\s{2,}(\d{3,4})\s+(.+?)\s{2,}\S+\s+\S+\s+(\$[\d,]+)\s+(.+?)\s*$', l)
        if m:
            st = m.group(7)
            out.append(lot('wegotland', 'Treeby Park', 'Anketell', m.group(2), m.group(3), m.group(4), m.group(5), m.group(6),
                           True if 'titled' in st.lower() else None, 'Available' if 'sale' in st.lower() else st, url, stage=m.group(1)))
    if not out: raise RuntimeError('no lots parsed')
    return out

SOURCES = [('parcelpricelist', src_parcel), ('satterley', src_satterley), ('spires', src_spires),
           ('parkland-heights', src_parkland), ('woodlands', src_woodlands), ('land4sale', src_land4sale),
           ('wegotland', src_wegotland)]

# ---------------- matching ----------------
BAD = re.compile(r'(?i)hold|offer|sold|reserv|contract|unavailable|deposit')
def match(l, b):
    if not l.get('suburb') or l['suburb'].lower() not in [s.lower() for s in b['suburbs']]: return False
    if l.get('price') is None or l['price'] > b['max_price']: return False
    if b.get('min_sqm') and (l.get('sqm') is None or l['sqm'] < b['min_sqm']): return False
    if b.get('max_sqm') and (l.get('sqm') is None or l['sqm'] > b['max_sqm']): return False
    if BAD.search(l.get('status') or ''): return False
    if b.get('titled_only') and l.get('titled') is not True: return False
    return True

def load_briefs():
    env = os.environ.get('BRIEFS_JSON', '').strip()
    raw = json.loads(env) if env else json.load(open(BRIEFS))
    if isinstance(raw, dict): raw = [raw]
    out = []
    for b in raw:
        if not b.get('active', True): continue
        b.setdefault('suburbs', []); b.setdefault('max_price', 10**12)
        if isinstance(b['suburbs'], str): b['suburbs'] = [b['suburbs']]
        out.append(b)
    return out

def main():
    show_all = '--all' in sys.argv
    briefs = load_briefs()
    seen = json.load(open(SEEN)) if os.path.exists(SEEN) else {'lots': {}, 'sources': {}}
    seen.setdefault('lots', {}); seen.setdefault('sources', {})
    STATE.update(seen); seen = STATE
    first = not seen['lots']
    t0 = time.time()
    report = {'run_at': NOW, 'timezone': 'Australia/Perth', 'first_run': first, 'briefs': briefs, 'sources': {}, 'alerts': [], 'current_matches': {b['name']: [] for b in briefs}}
    all_lots = []
    for name, fn in SOURCES:
        s = seen['sources'].setdefault(name, {'failures': 0})
        ts = time.time()
        try:
            lots = fn()
            if not lots: raise RuntimeError('0 lots parsed')
            all_lots += lots
            s.update(last_ok=NOW, failures=0, last_count=len(lots), last_error=None)
            report['sources'][name] = {'ok': True, 'lots': len(lots), 'secs': round(time.time() - ts, 1)}
        except Exception as e:
            s['failures'] = s.get('failures', 0) + 1; s['last_error'] = f'{type(e).__name__}: {e}'[:300]; s['last_fail'] = NOW
            report['sources'][name] = {'ok': False, 'error': s['last_error'], 'consecutive_failures': s['failures'],
                                       'last_ok': s.get('last_ok'), 'secs': round(time.time() - ts, 1)}
    for l in all_lots:
        key = f"{l['source']}|{l['estate']}|{l['lot']}"
        old = seen['lots'].get(key)
        m_now = [b['name'] for b in briefs if match(l, b)]
        for bn in m_now:
            report['current_matches'].setdefault(bn, []).append(l)
            prev = (old or {}).get('matched', [])
            reason = None
            if not old: reason = 'new lot'
            elif bn not in prev: reason = f"newly matches (was status={old.get('status')}, price={old.get('price')})"
            elif old.get('price') and l['price'] and l['price'] < old['price']: reason = f"price drop {old['price']} -> {l['price']}"
            elif old.get('status') != l.get('status'): reason = f"status {old.get('status')} -> {l.get('status')}"
            if reason: report['alerts'].append({'brief': bn, 'reason': reason, **l})
        seen['lots'][key] = {**l, 'matched': m_now, 'last_seen': NOW, 'first_seen': (old or {}).get('first_seen', NOW)}
    if LOG: report['notes'] = LOG
    report['duration_secs'] = round(time.time() - t0, 1)
    tmp = SEEN + '.tmp'; json.dump(seen, open(tmp, 'w'), indent=1); os.replace(tmp, SEEN)
    if not show_all: report.pop('current_matches')
    out = json.dumps(report, indent=1, default=str)
    if '--report' in sys.argv:
        open(sys.argv[sys.argv.index('--report') + 1], 'w').write(out)
    elif '--quiet' not in sys.argv:
        print(out)
    if '--quiet' in sys.argv:  # public-log-safe summary: no brief names, no lots
        print(f"scan at {NOW} Perth, {report['duration_secs']}s, first_run={first}")
        for n, r in report['sources'].items():
            print(f"  {n:18} {'OK  ' if r['ok'] else 'FAIL'} lots={r.get('lots', 0):<4} {r['secs']}s" + ('' if r['ok'] else f"  {r['error'][:150]}"))

if __name__ == '__main__':
    try: main()
    except Exception:
        tb = traceback.format_exc()
        if '--quiet' in sys.argv: print('FATAL:', tb.strip().splitlines()[-1])
        else: print(json.dumps({'run_at': NOW, 'fatal': tb}))
        sys.exit(1)
