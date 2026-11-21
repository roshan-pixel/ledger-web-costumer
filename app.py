import os
import sqlite3
import json
import datetime
from pathlib import Path
from flask import Flask, jsonify, request, render_template
import urllib.request
import urllib.parse

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
    try:
        c.execute("ALTER TABLE customer_invoices ADD COLUMN ds_code TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


init_db()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fetch_ledger(path, timeout=10):
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

@app.route('/api/ds_lookup')
def api_ds_lookup():
    ds_code = request.args.get('ds_code', '').strip().upper()
    if not ds_code:
        return jsonify({'error': 'ds_code required'}), 400
    data = _fetch_ledger(f'/api/customer?ds_code={urllib.parse.quote(ds_code)}')
    return jsonify(data)


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
