#!/usr/bin/env python3
"""Serve the Luxury Towel Supplier Database UI"""
import sqlite3
import json
import os
from flask import Flask, send_from_directory, jsonify

DB_PATH = '/data/luxury_towel_suppliers/suppliers.db'
UI_DIR = '/data/luxury_towel_suppliers/ui'

app = Flask(__name__)

@app.route('/')
def index():
    # Read the HTML template and inject data
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM suppliers ORDER BY id")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    html_path = os.path.join(UI_DIR, 'index.html')
    with open(html_path, 'r') as f:
        html = f.read()

    # Inject data as JSON into the placeholder
    data_json = json.dumps(rows, ensure_ascii=False)
    html = html.replace('/* DATA_PLACEHOLDER */', data_json)

    return html

@app.route('/api/suppliers')
def api_suppliers():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM suppliers ORDER BY id")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/suppliers/<int:id>')
def api_supplier(id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM suppliers WHERE id=?", (id,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({"error": "not found"}), 404

if __name__ == '__main__':
    print("🛁 Luxury Towel Supplier Database UI")
    print("Opening at: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)