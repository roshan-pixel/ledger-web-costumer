import os
import sqlite3
import json
import datetime
from pathlib import Path
from flask import Flask, jsonify, request, render_template
import urllib.request
import urllib.parse
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# URL of the main Ledger Web instance – set via env var in production
LEDGER_WEB_URL = os.environ.get('LEDGER_WEB_URL', 'https://ledger-web-app.onrender.com').rstrip('/')

DB_PATH = str(Path(__file__).parent / 'customer.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS customer_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT,
            ds_code TEXT DEFAULT '',
            customer_name TEXT,
            customer_phone TEXT,
            amount REAL DEFAULT 0,
            date_created TEXT,
            items TEXT,
            status TEXT DEFAULT 'pending',
            notes TEXT DEFAULT ''
        )
    ''')
    c.execute('CREATE TABLE IF NOT EXISTS ds_cache (ds_code TEXT PRIMARY KEY, ds_name TEXT, mobile TEXT, data TEXT)')
    try:
        c.execute('ALTER TABLE ds_cache ADD COLUMN data TEXT')
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE customer_invoices ADD COLUMN ds_code TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


init_db()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fetch_ledger(path, timeout=55):
    """Fetch JSON from the main Ledger Web API."""
    url = f"{LEDGER_WEB_URL}{path}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ledger-web-costumer/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {'error': str(e)}


def next_invoice_no():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM customer_invoices")
    n = c.fetchone()[0] + 1
    conn.close()
    today = datetime.date.today()
    return f"CINV-{today.strftime('%Y%m')}-{n:04d}"


# ── Page Routes ──────────────────────────────────────────────────────────────

@app.route('/')
@app.route('/invoice')
def invoice():
    return render_template('invoice.html')


@app.route('/inventory')
def inventory():
    return render_template('inventory.html')


@app.route('/disease_guide')
def disease_guide():
    return render_template('disease_guide.html')


# ── Health ────────────────────────────────────────────────────────────────────

@app.route('/ping')
@app.route('/health')
def ping():
    return jsonify({'status': 'ok', 'message': 'pong'}), 200


# ── Inventory API (live-proxied from main Ledger Web) ─────────────────────────

@app.route('/api/inventory')
def api_inventory():
    data = _fetch_ledger('/api/inventory')
    return jsonify(data)


@app.route('/api/stock_check')
def api_stock_check():
    """Return slim product list: name + remaining qty – for invoice autocomplete."""
    data = _fetch_ledger('/api/inventory')
    if 'error' in data:
        return jsonify(data), 502
    products = []
    for row in data.get('data', []):
        name = row.get('Product Name') or row.get('Item Name') or row.get('Product') or ''
        rem = row.get('Remaining Qty') or row.get('Remaining') or 0
        try:
            rem = float(str(rem).replace(',', ''))
        except Exception:
            rem = 0
        price = row.get('Price/Pc (Rs.)') or row.get('Price') or 0
        try:
            price = float(str(price).replace(',', ''))
        except Exception:
            price = 0
        if name and name.upper() != 'TOTAL':
            products.append({'name': name, 'remaining': rem, 'price': price})
    products.sort(key=lambda x: x['name'])
    return jsonify({'products': products})


# ── DS Lookup API (live-proxied from main Ledger Web) ────────────────────────

_cache = {}
_cache_lock = threading.Lock()


def _cached_fetch(path, ttl=600, timeout=25):
    """Fetch from ledger with an in-memory TTL cache (stale copy used if upstream fails)."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = _fetch_ledger(path, timeout=timeout)
    ok = not (isinstance(data, dict) and 'error' in data and len(data) == 1)
    if ok:
        with _cache_lock:
            _cache[path] = (now, data)
        return data
    return hit[1] if hit else data


def _warm_ledger():
    """Wake the (sleeping) main ledger and preload the lookup lists."""
    for path in ('/api/mizoram_bronze', '/api/invoice/list'):
        try:
            _cached_fetch(path, timeout=60)
        except Exception:
            pass


@app.route('/api/warm')
def api_warm():
    threading.Thread(target=_warm_ledger, daemon=True).start()
    return jsonify({'ok': True})


def _lookup_customer(ds_code):
    data = _cached_fetch(f'/api/customer?ds_code={urllib.parse.quote(ds_code)}', ttl=300)
    if isinstance(data, dict) and 'error' not in data:
        return data
    return None


def _lookup_bronze(ds_code):
    bronze = _cached_fetch('/api/mizoram_bronze')
    if isinstance(bronze, list):
        for row in bronze:
            if str(row.get('ds_id', '')).upper() == ds_code:
                return {'ds_code': row.get('ds_id', ''), 'ds_name': row.get('ds_name', ''),
                        'mobile': row.get('phone_no', '')}
    return None


def _lookup_invoices(ds_code):
    inv_data = _cached_fetch('/api/invoice/list')
    if isinstance(inv_data, list):
        for r in inv_data:
            if str(r.get('ds_code', '')).upper() == ds_code:
                return {'ds_code': ds_code, 'ds_name': r.get('customer_name', ''), 'mobile': ''}
    return None


def _cache_get(ds_code):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT ds_name, mobile, data FROM ds_cache WHERE ds_code=?", (ds_code,))
    row = c.fetchone(); conn.close()
    if not row:
        return None
    try:
        if row[2]:
            return json.loads(row[2])
    except Exception:
        pass
    return {'ds_code': ds_code, 'ds_name': row[0], 'mobile': row[1]}


def _cache_put(d):
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO ds_cache (ds_code, ds_name, mobile, data) VALUES (?,?,?,?)",
              (d['ds_code'], d.get('ds_name', ''), d.get('mobile', ''), json.dumps(d)))
    conn.commit(); conn.close()


@app.route('/api/ds_lookup')
def api_ds_lookup():
    ds_code = request.args.get('ds_code', '').strip().upper()
    if not ds_code:
        return jsonify({'error': 'ds_code required'}), 400

    # 1. Previously looked-up DS (instant)
    hit = _cache_get(ds_code)
    if hit:
        return jsonify(hit)

    # 2. Live AWPL portal lookup (headless browser, same method as Ledger Web)
    try:
        from ds_lookup_api import fetch_ds_from_portal
        data = fetch_ds_from_portal(ds_code)
        if data:
            _cache_put(data)
            return jsonify(data)
    except Exception as ex:
        print('Portal lookup error:', ex)

    # 3. Fallback: main Ledger Web sources, queried in parallel
    funcs = [_lookup_customer, _lookup_bronze, _lookup_invoices]
    results = [None] * len(funcs)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(f, ds_code): i for i, f in enumerate(funcs)}
        for fut in as_completed(futs):
            try:
                results[futs[fut]] = fut.result()
            except Exception:
                pass
    for r in results:
        if r:
            _cache_put(r)
            return jsonify(r)
    return jsonify({'error': 'DS Code not found'}), 404


# ── Disease Guide API (live-proxied from main Ledger Web) ─────────────────────

@app.route('/api/disease_guide')
def api_disease_guide():
    q = request.args.get('q', '')
    category = request.args.get('category', '')
    qs = urllib.parse.urlencode({k: v for k, v in [('q', q), ('category', category)] if v})
    path = '/api/disease_guide' + (f'?{qs}' if qs else '')
    data = _fetch_ledger(path)
    return jsonify(data)


@app.route('/api/disease_guide/<int:idx>')
def api_disease_guide_detail(idx):
    data = _fetch_ledger(f'/api/disease_guide/{idx}')
    return jsonify(data)


# ── Customer Invoice API ──────────────────────────────────────────────────────

@app.route('/api/customer_invoice', methods=['GET'])
def api_list_invoices():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM customer_invoices ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/customer_invoice', methods=['POST'])
def api_create_invoice():
    data = request.get_json()
    ds_code = (data.get('ds_code') or '').strip().upper()
    customer_name = (data.get('customer_name') or '').strip()
    customer_phone = (data.get('customer_phone') or '').strip()
    items = data.get('items', [])
    notes = (data.get('notes') or '').strip()

    if not customer_name:
        return jsonify({'error': 'customer_name required'}), 400
    if not items:
        return jsonify({'error': 'items required'}), 400

    total = sum(float(item.get('qty', 0)) * float(item.get('price', 0)) for item in items)
    invoice_no = next_invoice_no()
    date_created = datetime.datetime.now().isoformat()

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO customer_invoices
            (invoice_no, ds_code, customer_name, customer_phone, amount, date_created, items, status, notes)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (invoice_no, ds_code, customer_name, customer_phone, total,
          date_created, json.dumps(items), 'pending', notes))
    new_id = c.lastrowid
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'id': new_id, 'invoice_no': invoice_no, 'amount': total})


@app.route('/api/customer_invoice/<int:inv_id>', methods=['GET'])
def api_get_invoice(inv_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM customer_invoices WHERE id=?", (inv_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))




# ── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': '404 Not Found'}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': '500 Internal Server Error', 'msg': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001)
