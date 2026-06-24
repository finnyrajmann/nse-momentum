"""
NSE Momentum Swing Trader — DO Functions Entry Point
=====================================================
Monthly momentum rebalancing system based on Quantitative Momentum.

Scoring Formula:
    Score = (1M return + 3M return + 6M return) / 1M volatility

Two parallel systems tracked in same run:
    System A — 20 positions, exit rank 50  (book parameters)
    System B —  5 positions, exit rank 20  (capital-adjusted)

Rebalancing:
    - Runs on first available weekday between 19th-23rd of each month
    - Uses a state file (data/last_rebalance.txt) on GitHub to ensure
      exactly one run per month
    - If market is closed today, skips gracefully with email notification

Data sources:
    - Yahoo Finance API for OHLC price data
    - GitHub REST API for reading/writing CSV state
    - Gmail SMTP for HTML email reports

No pip installs needed — requests + standard library only.
"""

import os
import csv
import smtplib
import time
import base64
import math
from io import StringIO
from datetime import datetime, date
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# System A — book parameters
SYS_A = {
    'name':          'A',
    'label':         '20-position',
    'max_positions': 20,
    'exit_rank':     50,
    'positions_file': 'data/positions_momentum_20.csv',
    'log_file':       'data/trade_log_momentum_20.csv',
}

# System B — capital-adjusted parameters
SYS_B = {
    'name':          'B',
    'label':         '5-position',
    'max_positions': 5,
    'exit_rank':     20,
    'positions_file': 'data/positions_momentum_5.csv',
    'log_file':       'data/trade_log_momentum_5.csv',
}

SYSTEMS = [SYS_A, SYS_B]

POSITION_SIZE   = 10000   # fixed capital per position (paper)
SLEEP           = 0.3     # seconds between Yahoo Finance calls
MARKET_CHECK_SYM = 'RELIANCE'  # used to detect market holidays

# Lookback periods in trading days (approx)
DAYS_1M = 21
DAYS_3M = 63
DAYS_6M = 126

STATE_FILE = 'data/last_rebalance.txt'


# ─────────────────────────────────────────────
# YAHOO FINANCE
# ─────────────────────────────────────────────

def fetch_closes(symbol, period='1y'):
    """Fetch daily close prices for a symbol. Returns list of floats or None."""
    ticker  = symbol.upper().strip() + '.NS'
    url     = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
    params  = {'range': period, 'interval': '1d', 'events': 'history'}
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r    = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        res  = data['chart']['result'][0]
        closes = res['indicators']['quote'][0]['close']
        timestamps = res['timestamp']
        # filter None bars
        pairs = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
        return pairs
    except Exception as e:
        print(f"  {symbol}: fetch error — {e}")
        return None


def last_trading_date(pairs):
    """Return date of last bar as date object."""
    if not pairs:
        return None
    ts = pairs[-1][0]
    return datetime.utcfromtimestamp(ts).date()


def is_market_open():
    """Check if today is a trading day by comparing last bar date to today."""
    pairs = fetch_closes(MARKET_CHECK_SYM, period='5d')
    if not pairs:
        return False
    last_date = last_trading_date(pairs)
    today     = date.today()
    print(f"  Market check: last trading date = {last_date}, today = {today}")
    return last_date == today


# ─────────────────────────────────────────────
# MOMENTUM SCORING
# ─────────────────────────────────────────────

def compute_return(closes, lookback_days):
    """
    Compute return over last `lookback_days` trading bars.
    Returns percentage return or None if insufficient data.
    """
    if len(closes) < lookback_days + 1:
        return None
    price_now  = closes[-1]
    price_then = closes[-(lookback_days + 1)]
    if price_then == 0:
        return None
    return (price_now - price_then) / price_then * 100


def compute_volatility(closes, lookback_days):
    """
    Compute annualised daily return std dev over last `lookback_days` bars.
    Returns percentage volatility or None if insufficient data.
    """
    if len(closes) < lookback_days + 1:
        return None
    subset = closes[-(lookback_days + 1):]
    daily_returns = []
    for i in range(1, len(subset)):
        if subset[i-1] != 0:
            daily_returns.append((subset[i] - subset[i-1]) / subset[i-1] * 100)
    if len(daily_returns) < 5:
        return None
    n    = len(daily_returns)
    mean = sum(daily_returns) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / (n - 1)
    return math.sqrt(variance)


def score_stock(symbol):
    """
    Compute momentum score for one stock.
    Score = (1M_return + 3M_return + 6M_return) / 1M_volatility
    Returns dict with score and components, or None on failure.
    """
    pairs = fetch_closes(symbol, period='1y')
    time.sleep(SLEEP)
    if not pairs or len(pairs) < DAYS_6M + 5:
        return None

    closes = [c for _, c in pairs]

    r1m  = compute_return(closes, DAYS_1M)
    r3m  = compute_return(closes, DAYS_3M)
    r6m  = compute_return(closes, DAYS_6M)
    vol1m = compute_volatility(closes, DAYS_1M)

    if any(v is None for v in [r1m, r3m, r6m, vol1m]):
        return None
    if vol1m == 0:
        return None

    score = (r1m + r3m + r6m) / vol1m

    return {
        'symbol':  symbol,
        'score':   round(score, 4),
        'r1m':     round(r1m,  2),
        'r3m':     round(r3m,  2),
        'r6m':     round(r6m,  2),
        'vol1m':   round(vol1m, 4),
        'price':   round(closes[-1], 2),
    }


def rank_universe(watchlist):
    """
    Score and rank all stocks in watchlist.
    Returns list of score dicts sorted by score descending (rank 1 = best).
    """
    print(f"  Scoring {len(watchlist)} stocks...")
    scored = []
    for i, row in enumerate(watchlist):
        symbol = row['Symbol'].strip()
        result = score_stock(symbol)
        if result:
            scored.append(result)
        if (i + 1) % 50 == 0:
            print(f"  Progress: {i+1}/{len(watchlist)} scored: {len(scored)}")

    scored.sort(key=lambda x: x['score'], reverse=True)
    for i, s in enumerate(scored):
        s['rank'] = i + 1

    print(f"  Scored: {len(scored)} stocks successfully")
    return scored


# ─────────────────────────────────────────────
# REBALANCING LOGIC
# ─────────────────────────────────────────────

def rebalance_system(system, positions, trade_log, ranked):
    """
    Run FRR (Find-Remove-Replace) rebalancing for one system.

    Find:    positions whose current rank > exit_rank
    Remove:  exit those positions
    Replace: fill empty slots with top-ranked stocks not already held

    Returns: exits, new_entries, updated_positions, updated_trade_log
    """
    today       = datetime.now().strftime('%Y-%m-%d')
    max_pos     = system['max_positions']
    exit_rank   = system['exit_rank']
    label       = system['label']

    rank_map    = {s['symbol']: s for s in ranked}
    exits       = []
    new_entries = []

    # ── FIND & REMOVE ────────────────────────────────────────────────────────
    surviving   = []
    for pos in positions:
        symbol      = pos['Symbol']
        entry_price = float(pos['EntryPrice'])
        quantity    = int(pos['Quantity'])
        entry_date  = pos['EntryDate']
        days_held   = (datetime.now() - datetime.strptime(entry_date, '%Y-%m-%d')).days

        stock_data  = rank_map.get(symbol)
        current_rank = stock_data['rank'] if stock_data else 9999
        current_price = stock_data['price'] if stock_data else entry_price

        pnl     = round((current_price - entry_price) * quantity, 2)
        pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)

        if current_rank > exit_rank:
            exits.append({
                'Symbol':      symbol,
                'EntryDate':   entry_date,
                'EntryPrice':  entry_price,
                'Quantity':    quantity,
                'ExitDate':    today,
                'ExitPrice':   current_price,
                'PnL':         pnl,
                'PnL%':        pnl_pct,
                'DaysHeld':    days_held,
                'ExitReason':  f'Rank dropped to {current_rank} (exit threshold: {exit_rank})',
                'ExitRank':    current_rank,
                'System':      label,
            })
            trade_log.append({
                'Symbol':      symbol,
                'EntryDate':   entry_date,
                'EntryPrice':  entry_price,
                'Quantity':    quantity,
                'Capital':     round(entry_price * quantity, 2),
                'ExitDate':    today,
                'ExitPrice':   current_price,
                'PnL':         pnl,
                'PnL%':        pnl_pct,
                'DaysHeld':    days_held,
                'ExitReason':  f'Rank dropped to {current_rank}',
                'System':      label,
            })
            print(f"  [{label}] EXIT {symbol} — rank {current_rank} > {exit_rank} | P&L: ₹{pnl:+.0f}")
        else:
            surviving.append(pos)

    # ── REPLACE ───────────────────────────────────────────────────────────────
    held_symbols  = {p['Symbol'] for p in surviving}
    slots_to_fill = max_pos - len(surviving)

    candidates = [
        s for s in ranked
        if s['symbol'] not in held_symbols and s['rank'] <= max_pos
    ]

    for candidate in candidates:
        if slots_to_fill <= 0:
            break
        symbol   = candidate['symbol']
        price    = candidate['price']
        quantity = max(1, int(POSITION_SIZE / price))

        new_pos = {
            'Symbol':     symbol,
            'EntryDate':  today,
            'EntryPrice': price,
            'Quantity':   quantity,
            'Rank':       candidate['rank'],
            'Score':      candidate['score'],
            'System':     label,
            'TrackType':  'Paper',
        }
        surviving.append(new_pos)
        held_symbols.add(symbol)
        slots_to_fill -= 1

        new_entries.append({
            'symbol':  symbol,
            'price':   price,
            'rank':    candidate['rank'],
            'score':   candidate['score'],
            'r1m':     candidate['r1m'],
            'r3m':     candidate['r3m'],
            'r6m':     candidate['r6m'],
            'vol1m':   candidate['vol1m'],
            'quantity': quantity,
        })
        print(f"  [{label}] ENTRY {symbol} — rank {candidate['rank']} | score {candidate['score']}")

    return exits, new_entries, surviving, trade_log


# ─────────────────────────────────────────────
# GITHUB REST API
# ─────────────────────────────────────────────

def github_get(repo, path, pat):
    url     = f'https://api.github.com/repos/{repo}/contents/{path}'
    headers = {'Authorization': f'token {pat}',
               'Accept': 'application/vnd.github.v3+json'}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data    = r.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def github_put(repo, path, pat, content, sha, message):
    url     = f'https://api.github.com/repos/{repo}/contents/{path}'
    headers = {'Authorization': f'token {pat}',
               'Accept': 'application/vnd.github.v3+json'}
    payload = {
        'message': message,
        'content': base64.b64encode(content.encode('utf-8')).decode('utf-8'),
        'sha':     sha,
    }
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()


def parse_csv(content):
    reader = csv.DictReader(StringIO(content))
    return list(reader)


def to_csv(rows, fieldnames):
    out    = StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def already_ran_this_month(repo, pat):
    """Check state file to see if rebalancing already ran this month."""
    try:
        content, _ = github_get(repo, STATE_FILE, pat)
        last_run   = content.strip()
        if not last_run:
            return False
        last_date = datetime.strptime(last_run, '%Y-%m-%d')
        today     = datetime.now()
        return (last_date.year == today.year and
                last_date.month == today.month)
    except Exception:
        # File doesn't exist yet — first run
        return False


def update_state_file(repo, pat, today_str):
    """Write today's date to state file so we don't double-run."""
    try:
        _, sha = github_get(repo, STATE_FILE, pat)
        github_put(repo, STATE_FILE, pat, today_str, sha,
                   f'Rebalance run: {today_str}')
    except Exception:
        # File doesn't exist, create it via PUT with empty sha
        url     = f'https://api.github.com/repos/{repo}/contents/{STATE_FILE}'
        headers = {'Authorization': f'token {pat}',
                   'Accept': 'application/vnd.github.v3+json'}
        payload = {
            'message': f'Rebalance run: {today_str}',
            'content': base64.b64encode(today_str.encode()).decode(),
        }
        requests.put(url, headers=headers, json=payload, timeout=15)


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def ts():
    return 'border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;'

def ths():
    return 'background:#2c3e50;color:#fff;padding:8px 12px;text-align:left;'

def tds(align='left'):
    return f'padding:7px 12px;border-bottom:1px solid #eee;text-align:{align};'

def section(title):
    return f'<h3 style="color:#2c3e50;margin:24px 0 8px 0;">{title}</h3>'


def build_system_html(sys_config, exits, new_entries, positions, ranked):
    """Build HTML block for one system's results."""
    label    = sys_config['label']
    max_pos  = sys_config['max_positions']
    ex_rank  = sys_config['exit_rank']
    rank_map = {s['symbol']: s for s in ranked}

    html = f'<hr style="border:1px solid #ddd;margin:32px 0;">'
    html += f'<h2 style="color:#2c3e50;">System {sys_config["name"]} — {label} '\
            f'<small style="font-size:14px;color:#888;">(max {max_pos} positions | exit rank {ex_rank})</small></h2>'

    # Exits
    html += section(f'✅ Exits This Month ({len(exits)})') if exits else section('✅ Exits: None this month')
    if exits:
        html += f'<table style="{ts()}"><thead><tr>'
        for col in ['', 'Symbol', 'Entry ₹', 'Exit ₹', 'P&L %', 'P&L ₹', 'Days', 'Exit Reason']:
            html += f'<th style="{ths()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in exits:
            icon = '🟢' if r['PnL'] >= 0 else '🔴'
            html += f'''<tr>
                <td style="{tds()}">{icon}</td>
                <td style="{tds()}"><b>{r['Symbol']}</b></td>
                <td style="{tds('right')}">₹{float(r['EntryPrice']):.2f}</td>
                <td style="{tds('right')}">₹{float(r['ExitPrice']):.2f}</td>
                <td style="{tds('right')}">{float(r["PnL%"]):+.2f}%</td>
                <td style="{tds('right')}">₹{float(r["PnL"]):+.0f}</td>
                <td style="{tds('right')}">{r['DaysHeld']}d</td>
                <td style="{tds()}">{r['ExitReason']}</td>
            </tr>'''
        html += '</tbody></table>'

    # New entries
    html += section(f'🔔 New Entries ({len(new_entries)})') if new_entries else section('🔔 New Entries: None')
    if new_entries:
        html += f'<table style="{ts()}"><thead><tr>'
        for col in ['Rank', 'Symbol', 'Price ₹', 'Score', '1M %', '3M %', '6M %', '1M Vol']:
            html += f'<th style="{ths()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for e in new_entries:
            html += f'''<tr>
                <td style="{tds('right')}">{e['rank']}</td>
                <td style="{tds()}"><b>{e['symbol']}</b></td>
                <td style="{tds('right')}">₹{e['price']}</td>
                <td style="{tds('right')}">{e['score']}</td>
                <td style="{tds('right')}">{e['r1m']:+.2f}%</td>
                <td style="{tds('right')}">{e['r3m']:+.2f}%</td>
                <td style="{tds('right')}">{e['r6m']:+.2f}%</td>
                <td style="{tds('right')}">{e['vol1m']:.4f}</td>
            </tr>'''
        html += '</tbody></table>'

    # Open positions
    if positions:
        total_pnl = 0
        rows = []
        for pos in positions:
            symbol      = pos['Symbol']
            entry_price = float(pos['EntryPrice'])
            quantity    = int(pos['Quantity'])
            entry_date  = pos['EntryDate']
            days_held   = (datetime.now() - datetime.strptime(entry_date, '%Y-%m-%d')).days
            stock_data  = rank_map.get(symbol)
            cur_price   = stock_data['price'] if stock_data else entry_price
            cur_rank    = stock_data['rank']  if stock_data else '—'
            pnl         = round((cur_price - entry_price) * quantity, 2)
            pnl_pct     = round((cur_price - entry_price) / entry_price * 100, 2)
            total_pnl  += pnl
            rows.append({
                'symbol': symbol, 'entry': entry_price, 'price': cur_price,
                'pnl': pnl, 'pnl_pct': pnl_pct, 'days': days_held,
                'rank': cur_rank, 'qty': quantity,
            })

        pnl_color = '#27ae60' if total_pnl >= 0 else '#e74c3c'
        html += section(
            f'📋 Open Positions ({len(positions)}) &nbsp;|&nbsp; '
            f'Total P&L: <span style="color:{pnl_color}">₹{total_pnl:+.0f}</span>'
        )
        html += f'<table style="{ts()}"><thead><tr>'
        for col in ['', 'Symbol', 'Entry ₹', 'Price ₹', 'P&L %', 'P&L ₹', 'Rank', 'Days']:
            html += f'<th style="{ths()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in rows:
            icon = '🟢' if r['pnl'] >= 0 else '🔴'
            html += f'''<tr>
                <td style="{tds()}">{icon}</td>
                <td style="{tds()}"><b>{r['symbol']}</b></td>
                <td style="{tds('right')}">₹{r['entry']:.2f}</td>
                <td style="{tds('right')}">₹{r['price']:.2f}</td>
                <td style="{tds('right')}">{r['pnl_pct']:+.2f}%</td>
                <td style="{tds('right')}">₹{r['pnl']:+.0f}</td>
                <td style="{tds('right')}">{r['rank']}</td>
                <td style="{tds('right')}">{r['days']}d</td>
            </tr>'''
        html += '</tbody></table>'
    else:
        html += section('📋 Open Positions: None')

    return html


def send_email(sys_a_data, sys_b_data, ranked, skipped=False, skip_reason=''):
    sender    = os.environ.get('GMAIL_SENDER')
    password  = os.environ.get('GMAIL_APP_PASSWORD')
    recipient = os.environ.get('GMAIL_RECIPIENT')
    repo_name = os.environ.get('GITHUB_REPO')
    today     = datetime.now().strftime('%d %b %Y')

    if skipped:
        subject = f'NSE Momentum — {today} | Skipped: {skip_reason}'
        html    = f'''
        <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
        <h2 style="background:#2c3e50;color:#fff;padding:14px 18px;margin:0;border-radius:4px 4px 0 0;">
            📊 NSE Momentum Rebalancer — {today}
        </h2>
        <p style="padding:16px;color:#888;">⏭️ Skipped — {skip_reason}</p>
        </div>'''
    else:
        a_exits, a_entries, a_positions = sys_a_data
        b_exits, b_entries, b_positions = sys_b_data
        total_entries = len(a_entries) + len(b_entries)
        total_exits   = len(a_exits)   + len(b_exits)

        subject = (f'NSE Momentum — {today} | '
                   f'{total_entries} new | {total_exits} exits | '
                   f'A:{len(a_positions)} B:{len(b_positions)} open')

        html = f'''
        <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
        <h2 style="background:#2c3e50;color:#fff;padding:14px 18px;margin:0;border-radius:4px 4px 0 0;">
            📊 NSE Momentum Rebalancer — {today}
        </h2>
        <p style="padding:8px 0 0 0;color:#555;font-size:13px;">
            Formula: (1M + 3M + 6M return) / 1M volatility &nbsp;|&nbsp;
            Universe: {len(ranked)} ranked stocks
        </p>
        '''
        html += build_system_html(SYS_A, a_exits, a_entries, a_positions, ranked)
        html += build_system_html(SYS_B, b_exits, b_entries, b_positions, ranked)
        html += f'''
        <p style="margin-top:24px;font-size:12px;color:#888;">
            <a href="https://github.com/{repo_name}/blob/main/data/trade_log_momentum_a.csv"
               style="color:#2c3e50;">Trade log A</a> &nbsp;|&nbsp;
            <a href="https://github.com/{repo_name}/blob/main/data/trade_log_momentum_b.csv"
               style="color:#2c3e50;">Trade log B</a><br>
            — NSE Momentum Trader (automated)
        </p>
        </div>'''

    msg            = MIMEMultipart()
    msg['From']    = sender
    msg['To']      = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f'  Email sent → {recipient}')


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main(args):
    print('\n' + '='*55)
    print('  NSE MOMENTUM REBALANCER — DO Functions Run')
    print('='*55)

    pat       = os.environ.get('GITHUB_PAT')
    repo_name = os.environ.get('GITHUB_REPO')
    today_str = datetime.now().strftime('%Y-%m-%d')

    try:
        # ── Guard: already ran this month? ────────────────────────────────────
        print('\n[1/6] Checking rebalance state...')
        if already_ran_this_month(repo_name, pat):
            print('  Already rebalanced this month — skipping.')
            send_email(None, None, [], skipped=True,
                       skip_reason='Already rebalanced this month')
            return {'statusCode': 200, 'body': 'Already ran this month'}

        # ── Guard: is the market open today? ──────────────────────────────────
        print('\n[2/6] Checking market status...')
        if not is_market_open():
            print('  Market closed today — skipping.')
            send_email(None, None, [], skipped=True,
                       skip_reason='Market holiday or weekend')
            return {'statusCode': 200, 'body': 'Market closed'}

        # ── Load watchlist ────────────────────────────────────────────────────
        print('\n[3/6] Loading watchlist and scoring universe...')
        wl_content, _ = github_get(repo_name, 'data/watchlist.csv', pat)
        watchlist      = parse_csv(wl_content)
        print(f'  Watchlist: {len(watchlist)} symbols')

        # ── Rank universe ─────────────────────────────────────────────────────
        ranked = rank_universe(watchlist)
        if len(ranked) < 50:
            msg = f'Only {len(ranked)} stocks scored — too few to rank safely'
            print(f'  ERROR: {msg}')
            send_email(None, None, [], skipped=True, skip_reason=msg)
            return {'statusCode': 500, 'body': msg}

        # ── Load positions and trade logs for both systems ────────────────────
        print('\n[4/6] Loading positions and running rebalance...')

        pos_fields = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity',
                      'Rank', 'Score', 'System', 'TrackType']
        log_fields = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'Capital',
                      'ExitDate', 'ExitPrice', 'PnL', 'PnL%', 'DaysHeld',
                      'ExitReason', 'System']

        results  = {}
        sha_map  = {}

        for sys in SYSTEMS:
            pf   = sys['positions_file']
            lf   = sys['log_file']
            name = sys['name']

            pos_content, pos_sha = github_get(repo_name, pf, pat)
            log_content, log_sha = github_get(repo_name, lf, pat)

            positions  = parse_csv(pos_content)
            trade_log  = parse_csv(log_content)

            print(f'\n  System {name} ({sys["label"]}): '
                  f'{len(positions)} open positions')

            exits, new_entries, positions, trade_log = rebalance_system(
                sys, positions, trade_log, ranked
            )

            results[name] = {
                'exits':      exits,
                'entries':    new_entries,
                'positions':  positions,
                'trade_log':  trade_log,
            }
            sha_map[name] = {
                'pos_sha': pos_sha,
                'log_sha': log_sha,
                'pf':      pf,
                'lf':      lf,
            }

        # ── Sync to GitHub ────────────────────────────────────────────────────
        print('\n[5/6] Syncing to GitHub...')
        commit_msg = f'Momentum rebalance — {today_str}'

        for sys in SYSTEMS:
            name = sys['name']
            r    = results[name]
            s    = sha_map[name]

            github_put(repo_name, s['pf'], pat,
                       to_csv(r['positions'], pos_fields),
                       s['pos_sha'], commit_msg)

            github_put(repo_name, s['lf'], pat,
                       to_csv(r['trade_log'], log_fields),
                       s['log_sha'], commit_msg)

        # Update state file
        update_state_file(repo_name, pat, today_str)
        print('  State file updated.')

        # ── Send email ────────────────────────────────────────────────────────
        print('\n[6/6] Sending email...')
        a = results['A']
        b = results['B']
        send_email(
            (a['exits'], a['entries'], a['positions']),
            (b['exits'], b['entries'], b['positions']),
            ranked
        )

        print('\n  Done.\n')
        return {'statusCode': 200, 'body': 'Rebalance complete'}

    except Exception as e:
        import traceback
        print(f'\n  ERROR: {str(e)}')
        print(traceback.format_exc())
        return {'statusCode': 500, 'body': str(e)}
