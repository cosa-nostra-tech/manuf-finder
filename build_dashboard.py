#!/usr/bin/env python3
"""Build the Atelier outreach dashboard with conversation viewer."""

import sqlite3, json, re, base64

DB_PATH = "/data/luxury_towel_suppliers/suppliers.db"
OUT_DIR = "/data/luxury_towel_suppliers/deploy"

COUNTRY_KEYWORDS = {
    "CN": ["shandong", "jiangsu", "zhejiang", "fujian", "hebei", "guangdong",
           "gaomi", "gaoyang", "changzhou", "nantong", "ningbo", "zibo",
           "qingdao", "jinjiang", "wuxi", "china"],
    "JP": ["imabari", "ehime", "osaka", "senshu", "kyoto", "japan"],
    "TW": ["yunlin", "huwei", "dounan", "taiwan"],
    "VN": ["ho chi minh", "hai phong", "binh duong", "vietnam", "dong nai"],
    "TH": ["bangkok", "samut", "thailand"],
    "ID": ["bandung", "java", "semarang", "solo", "indonesia"],
    "MY": ["penang", "selangor", "malaysia"],
}


def detect_country(text):
    if not text:
        return "UNKNOWN"
    t = text.lower()
    for code, keywords in COUNTRY_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return code
    return "UNKNOWN"


def replace_const(html, name, data):
    """Replace a const declaration safely (avoids re.sub template interpretation)."""
    json_str = json.dumps(data, ensure_ascii=True)
    pattern = re.compile(rf'const {name} = .*?;\n', re.DOTALL)
    replacement = f'const {name} = {json_str};\n'
    return pattern.sub(lambda m: replacement, html, count=1)


def build():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM suppliers ORDER BY qualification_score DESC")
    suppliers = []
    for r in cur.fetchall():
        s = dict(r)
        s['_country'] = detect_country(s.get('factory_locations', ''))
        suppliers.append(s)

    cur.execute("SELECT * FROM conversation_log ORDER BY supplier_id, timestamp")
    convos = {}
    for r in cur.fetchall():
        sid = r['supplier_id']
        if sid not in convos:
            convos[sid] = []
        convos[sid].append({
            "id": r['id'],
            "supplier_id": r['supplier_id'],
            "channel": r['channel'],
            "direction": r['direction'],
            "content": r['content'],
            "timestamp": r['timestamp'],
            "agent_action": r['agent_action'] or '',
            "approval_status": r['approval_status'] or 'DRAFT'
        })

    translations = {}
    for sid, msgs in convos.items():
        for m in msgs:
            if m['channel'] == 'wechat' and m['direction'] == 'outbound':
                # Find the supplier name, certs, and MOQ for proper translation
                sup = next((s for s in suppliers if s['id'] == sid), None)
                supplier_name = sup['trade_name'] if sup else ''
                certs = sup.get('certs_and_audits', '') or '' if sup else ''
                moq = sup.get('moq', '') or '' if sup else 'TBD'
                trans = (
                    "Hello! I'm Jack, Head of Product Development at Atelier.\n"
                    "\n"
                    "We aggregate order flow from the world's largest brands and retailers. "
                    "We\u2019re not a traditional trading company \u2014 we\u2019re an order aggregation platform, "
                    "helping quality factories reach the largest orders via the shortest path.\n"
                    "\n"
                    f"We noticed {supplier_name} holds {certs} certifications, and your experience "
                    "with well-known brands gives us great confidence. Your extensive export market "
                    "experience is also something we value. We are currently looking for a long-term "
                    "partner for the North American premium hotel channel. Specific requirements:\n"
                    "\n"
                    "\u2022 700\u2013800gsm premium bath towels (Egyptian cotton/long-staple cotton blend)\n"
                    "\u2022 Custom jacquard hotel logos\n"
                    "\u2022 Annual demand of 500,000+ pieces\n"
                    "\n"
                    "A few quick questions:\n"
                    f"1. What\u2019s your MOQ? (We see {moq} \u2014 is that negotiable?)\n"
                    "2. Can you provide 700+ GSM samples?\n"
                    "3. Sample cost and lead time?\n"
                    "\n"
                    "If interested, we can move quickly. Sign NDA, then discuss specs.\n"
                    "\n"
                    "Jack\n"
                    "Head of Product Development | Atelier\n"
                    "jack@atelier.co\n"
                    "atelier.co"
                )
                translations[str(m['id'])] = trans

    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="240" viewBox="0 0 400 240"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#1e293b"/><stop offset="100%" stop-color="#0f172a"/></linearGradient></defs><rect width="400" height="240" fill="url(#g)"/><text x="200" y="100" text-anchor="middle" font-size="64">&#x1F9D6;</text><text x="200" y="155" text-anchor="middle" font-family="system-ui" font-size="14" fill="rgba(255,255,255,0.4)" font-weight="600">LUXURY TOWELS</text><text x="200" y="180" text-anchor="middle" font-family="system-ui" font-size="10" fill="rgba(255,255,255,0.2)">Hotel &amp; Resort Collection</text></svg>'
    svg_bytes = svg.encode('utf-8', errors='replace')
    svg_b64 = base64.b64encode(svg_bytes).decode()
    image_uri = f"data:image/svg+xml;base64,{svg_b64}"

    flows = [{
        "id": "luxury-towels-v1",
        "name": "Towels",
        "full_name": "Luxury Towels \u2014 Hotel & Resort",
        "category": "towels",
        "status": "active",
        "icon": "\ud83e\uddd6",
        "image": image_uri,
        "supplier_ids": [s['id'] for s in suppliers],
        "total_target": 50,
        "description": "Sourcing premium hotel-grade towels from Asia\u2019s top manufacturers"
    }]

    conn.close()

    with open(f"{OUT_DIR}/index.html", "r") as f:
        html = f.read()

    html = replace_const(html, 'DATA', suppliers)
    html = replace_const(html, 'CONVOS', convos)
    html = replace_const(html, 'TRANSLATIONS', translations)
    html = replace_const(html, 'FLOWS', flows)

    with open(f"{OUT_DIR}/index.html", "w") as f:
        f.write(html)

    print(f"\u2713 Built dashboard with {len(suppliers)} suppliers, {sum(len(v) for v in convos.values())} messages")
    print(f"  Output: {OUT_DIR}/index.html")


if __name__ == "__main__":
    build()
