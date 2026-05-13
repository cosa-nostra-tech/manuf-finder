#!/usr/bin/env python3
"""
Supplier Outreach Agent Engine (SOE) — Atelier
================================================
Human-in-the-loop outreach: Draft → Review → Approve → Send

Commands:
  draft         Generate personalized drafts for pending suppliers
  review        Show pending drafts with approve/reject options
  approve       Mark specific drafts as approved (by msg ID or supplier ID)
  reject        Mark specific drafts as rejected
  send          Send all APPROVED messages
  status        Show pipeline overview
  full-pipeline Draft + present for review in one step

Usage:
  python outreach_agent.py draft                    # Draft all pending
  python outreach_agent.py draft --id 1,5,14        # Draft specific suppliers
  python outreach_agent.py review                   # Show all DRAFT messages
  python outreach_agent.py approve --msg 40,41,44   # Approve by message ID
  python outreach_agent.py approve --supplier 1,14  # Approve by supplier ID
  python outreach_agent.py reject --msg 42          # Reject a draft
  python outreach_agent.py send                     # Send all APPROVED
  python outreach_agent.py status                   # Pipeline overview
  python outreach_agent.py full-pipeline            # Draft + review summary
"""

import sqlite3
import argparse
import json
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

DB_PATH = "/data/luxury_towel_suppliers/suppliers.db"

# ─── Atelier Persona ────────────────────────────────────────────────────────
BRAND = {
    "company": "Atelier",
    "company_cn": "Atelier",
    "sender": "Jack",
    "sender_cn": "Jack",
    "title": "Head of Product Development",
    "title_cn": "产品开发负责人",
    "email": "jack@atelier.co",
    "website": "atelier.co",
    "positioning": "aggregated order flow from the world's largest brands and retailers",
    "positioning_cn": "我们汇聚了全球最大品牌和零售商的采购需求",
    "value_prop": "Shortest path to the largest orders",
    "value_prop_cn": "最快路径获取最大订单",
    "tone": "friendly, direct, decision-maker, aligned interests",
}

# ─── Channel strategy ──────────────────────────────────────────────────────
CHANNEL_STRATEGY = {
    "CN": {"primary": "wechat", "secondary": "email", "language": "zh"},
    "TW": {"primary": "email", "secondary": "wechat", "language": "zh_tw"},
    "JP": {"primary": "email", "secondary": "phone",  "language": "en"},
    "VN": {"primary": "email", "secondary": "zalo",   "language": "en"},
    "TH": {"primary": "email", "secondary": "line",   "language": "en"},
    "ID": {"primary": "email", "secondary": "whatsapp","language": "en"},
    "MY": {"primary": "email", "secondary": "whatsapp","language": "en"},
}

COUNTRY_KEYWORDS = {
    "CN": ["shandong", "jiangsu", "zhejiang", "fujian", "hebei", "guangdong",
           "gaomi", "gaoyang", "changzhou", "nantong", "ningbo", "zibo",
           "qingdao", "jinjiang", "wuxi", "china", "中国"],
    "JP": ["imabari", "ehime", "osaka", "senshu", "kyoto", "japan", "日本"],
    "TW": ["yunlin", "huwei", "dounan", "taiwan", "台湾", "臺灣"],
    "VN": ["ho chi minh", "hai phong", "binh duong", "vietnam", "dong nai"],
    "TH": ["bangkok", "samut", "thailand"],
    "ID": ["bandung", "java", "semarang", "solo", "indonesia"],
    "MY": ["penang", "selangor", "malaysia"],
}


# ─── Helpers ────────────────────────────────────────────────────────────────

def detect_country(factory_locations: str) -> str:
    if not factory_locations:
        return "UNKNOWN"
    text = factory_locations.lower()
    for code, keywords in COUNTRY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return code
    return "UNKNOWN"


def get_strategy(country: str) -> dict:
    return CHANNEL_STRATEGY.get(country, {"primary": "email", "secondary": "email", "language": "en"})


def extract_certs(certs_text: str) -> list:
    if not certs_text:
        return []
    return [c.strip() for c in re.split(r'[,;|/]', certs_text) if c.strip() and len(c.strip()) > 2]


def format_moq(moq_text: str) -> str:
    return moq_text.strip() if moq_text else "TBD"


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


# ─── Message generators ────────────────────────────────────────────────────

def generate_wechat_message(supplier: dict, brief: dict = None) -> str:
    name = supplier.get('trade_name', '')
    certs = extract_certs(supplier.get('certs_and_audits', ''))
    brands = supplier.get('brands_worked_with', '')
    moq = format_moq(supplier.get('moq', ''))
    experience = supplier.get('market_experience', '')

    # Default brief for backward compatibility
    if not brief:
        brief = {
            "product_name": "premium hotel towels",
            "category": "Home Textiles",
            "description": "700–800gsm高端浴巾，埃及棉/长绒棉混纺，定制提花酒店Logo，年需求量50万条以上",
        }

    product_name = brief.get("product_name", "相关产品")
    desc = brief.get("description", "")
    category = brief.get("category", "")

    # Build product-specific description from the brief
    product_desc = ""
    if desc:
        # Take first 150 chars of description, trim at sentence boundary
        product_desc = desc[:150]
        if len(desc) > 150:
            product_desc = product_desc.rsplit("。", 1)[0] if "。" in product_desc else product_desc.rsplit(".", 1)[0] if "." in product_desc else product_desc

    cert_zh = {
        "OEKO-TEX": "OEKO-TEX Standard 100", "GOTS": "GOTS有机认证",
        "WRAP": "WRAP认证", "BSCI": "BSCI验厂", "ISO 9001": "ISO 9001",
        "ISO 14001": "ISO 14001", "SEDEX": "SEDEX验厂", "GRS": "GRS认证",
        "cGMP": "cGMP认证", "GMP": "GMP认证", "FDA": "FDA注册",
        "FDA Registered": "FDA注册",
    }
    cert_str = ""
    if certs:
        names = [cert_zh.get(c, c) for c in certs[:3]]
        cert_str = f"我们关注到{name}持有{'、'.join(names)}认证，"

    brand_str = ""
    if brands and brands not in ('', 'Not disclosed', 'Various', 'N/A'):
        brand_str = "贵司服务过的知名品牌也让我们很有信心，"

    exp_str = ""
    if experience and len(experience) > 5:
        exp_str = f"贵司在出口市场的丰富经验也是我们看重的，"

    return f"""你好！我是{BRAND['company_cn']}的{BRAND['title_cn']}{BRAND['sender_cn']}。

{BRAND['positioning_cn']}。我们不是传统贸易公司——我们是订单聚合平台，帮优质工厂以{BRAND['value_prop_cn']}。

{cert_str}{brand_str}{exp_str}我们目前正在寻找{product_name}的长期合作伙伴，具体需求：

{product_desc}

想简单了解几个问题：
1. 贵司MOQ？{f'（我们看到是{moq}，能否协商？）' if moq != 'TBD' else ''}
2. 能否提供样品？
3. 样品费用和交期？

如果感兴趣，我们可以很快推进。签NDA后详谈规格。

{BRAND['sender_cn']}
{BRAND['title_cn']} | {BRAND['company_cn']}
{BRAND['email']}
{BRAND['website']}""".strip()


def generate_email_message(supplier: dict, brief: dict = None) -> str:
    name = supplier.get('trade_name', '')
    certs = extract_certs(supplier.get('certs_and_audits', ''))
    brands = supplier.get('brands_worked_with', '')
    moq = format_moq(supplier.get('moq', ''))
    experience = supplier.get('market_experience', '')
    contact = supplier.get('contact_name', '')
    country = supplier.get('_country', 'UNKNOWN')

    # Default brief for backward compatibility
    if not brief:
        brief = {
            "product_name": "premium hotel towels",
            "category": "Home Textiles",
            "description": "700-800 GSM Egyptian/long-staple cotton blend, custom jacquard hotel logos, 500,000+ units/year, 4-5 star hotels & resorts",
        }

    product_name = brief.get("product_name", "our product line")
    desc = brief.get("description", "")
    category = brief.get("category", "")
    certs_required = brief.get("certifications_required", "")

    # Build product bullet points from the brief
    product_bullets = ""
    if desc:
        # Split on common delimiters and make bullet points
        points = [p.strip() for p in re.split(r'[.,;•]', desc) if p.strip() and len(p.strip()) > 5]
        product_bullets = "\n".join(f"• {p}" for p in points[:5])

    cert_str = ""
    if certs:
        cert_str = (f"I noticed {name} holds {', '.join(certs[:3])} certification"
                    f"{'s' if len(certs) > 1 else ''}, which tells me you take quality seriously.")
    elif certs_required:
        cert_str = f"We require {certs_required} — is your facility certified?"

    brand_str = ""
    if brands and brands not in ('', 'Not disclosed', 'Various', 'N/A'):
        brand_str = "Your work with major brands speaks for itself,"

    if country == "JP":
        intro = f"""Hi {contact or f'{name} Team'},

I'm {BRAND['sender']}, {BRAND['title']} at {BRAND['company']}. We aggregate order flow from the world's largest brands and retailers — and we have deep respect for the manufacturing tradition and quality your region represents."""
    else:
        intro = f"""Hi {contact or f'{name} Team'},

I'm {BRAND['sender']}, {BRAND['title']} at {BRAND['company']}. We aggregate order flow from the world's largest brands and retailers — meaning we can bring you more volume, faster, than any single buyer."""

    body = f"""We're looking for a manufacturing partner for {product_name}:

{product_bullets}

{cert_str}
{brand_str}

A few quick questions:
1. What's your MOQ? {f'(I saw {moq} on your profile — is that flexible?)' if moq != 'TBD' else ''}
2. Can you provide samples?
3. Sample lead time and cost?
4. Open to signing an NDA before we share detailed specs?

If this sounds interesting, I'd love to hop on a quick call. We move fast — and I think there's a strong fit here.

Best,
{BRAND['sender']}
{BRAND['title']} | {BRAND['company']}
{BRAND['email']}
{BRAND['website']}"""

    return f"{intro}\n\n{body}".strip()


def generate_email_subject(supplier: dict, brief: dict = None) -> str:
    name = supplier.get('trade_name', '')
    certs = extract_certs(supplier.get('certs_and_audits', ''))
    country = supplier.get('_country', 'UNKNOWN')

    product_name = brief.get("product_name", "Manufacturing") if brief else "Hotel Towel Manufacturing"

    if country == "JP":
        return f"{product_name.title()} Partnership — {name}"
    if certs:
        return f"{product_name.title()} — {name} ({certs[0]} Certified)"
    return f"{product_name.title()} — {name}"


# ─── DB operations ───────────────────────────────────────────────────────────

def get_pending_suppliers(db, supplier_ids=None):
    cur = db.cursor()
    if supplier_ids:
        placeholders = ','.join('?' * len(supplier_ids))
        cur.execute(f"""
            SELECT s.* FROM suppliers s
            LEFT JOIN conversation_log cl ON s.id = cl.supplier_id AND cl.agent_action = 'DRAFT'
            WHERE s.id IN ({placeholders})
            AND s.outreach_state IN ('ENRICHED', 'DISCOVERED')
            AND cl.id IS NULL
        """, supplier_ids)
    else:
        cur.execute("""
            SELECT s.* FROM suppliers s
            LEFT JOIN conversation_log cl ON s.id = cl.supplier_id AND cl.agent_action = 'DRAFT'
            WHERE s.outreach_state IN ('ENRICHED', 'DISCOVERED')
            AND cl.id IS NULL
            ORDER BY s.qualification_score DESC, s.data_completeness_score DESC
        """)
    return [dict(row) for row in cur.fetchall()]


def log_draft(db, supplier_id, channel, content, subject=None):
    cur = db.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur.execute("""
        INSERT INTO conversation_log (supplier_id, channel, direction, content, timestamp, agent_action, approval_status)
        VALUES (?, ?, 'outbound', ?, ?, 'DRAFT', 'DRAFT')
    """, (supplier_id, channel, content, now))
    db.commit()
    msg_id = cur.lastrowid

    # Store subject separately in a metadata field if needed
    if subject:
        # We'll add subject to the content header for email drafts
        updated_content = f"[Subject: {subject}]\n\n{content}"
        cur.execute("UPDATE conversation_log SET content = ? WHERE id = ?", (updated_content, msg_id))
        db.commit()

    return msg_id


def update_approval(db, msg_ids, status, approved_by="user"):
    cur = db.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    placeholders = ','.join('?' * len(msg_ids))
    cur.execute(f"""
        UPDATE conversation_log
        SET approval_status = ?, approved_by = ?, approved_at = ?
        WHERE id IN ({placeholders}) AND approval_status = 'DRAFT'
    """, [status, approved_by, now] + msg_ids)
    db.commit()
    return cur.rowcount


def get_drafts(db, status='DRAFT'):
    cur = db.cursor()
    cur.execute("""
        SELECT cl.*, s.trade_name, s.email, s.factory_locations
        FROM conversation_log cl
        JOIN suppliers s ON cl.supplier_id = s.id
        WHERE cl.approval_status = ?
        ORDER BY cl.timestamp DESC
    """, (status,))
    return [dict(row) for row in cur.fetchall()]


def get_drafts_by_supplier(db, supplier_ids, status='DRAFT'):
    cur = db.cursor()
    placeholders = ','.join('?' * len(supplier_ids))
    cur.execute(f"""
        SELECT cl.*, s.trade_name, s.email, s.factory_locations
        FROM conversation_log cl
        JOIN suppliers s ON cl.supplier_id = s.id
        WHERE cl.approval_status = ? AND cl.supplier_id IN ({placeholders})
        ORDER BY cl.timestamp DESC
    """, [status] + supplier_ids)
    return [dict(row) for row in cur.fetchall()]


def update_supplier_state(db, supplier_id, new_state, channel):
    cur = db.cursor()
    now = datetime.now().strftime('%Y-%m-%d')
    cur.execute("""
        UPDATE suppliers SET
            outreach_state = ?,
            outreach_channel = ?,
            first_contact_date = COALESCE(first_contact_date, ?),
            last_contact_date = ?,
            outreach_sequence = 'first_contact'
        WHERE id = ?
    """, (new_state, channel, now, now, supplier_id))
    db.commit()


# ─── SMTP ────────────────────────────────────────────────────────────────────

def send_email(to_addr: str, subject: str, body: str) -> bool:
    smtp_host = Path("/data/luxury_towel_suppliers/.smtp_config.json")
    if smtp_host.exists():
        cfg = json.loads(smtp_host.read_text())
    else:
        print("  ⚠ No SMTP config at /data/luxury_towel_suppliers/.smtp_config.json")
        return False

    msg = MIMEMultipart()
    msg['From'] = f"{BRAND['sender']} <{BRAND['email']}>"
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        with smtplib.SMTP(cfg['host'], cfg.get('port', 587)) as server:
            server.starttls()
            server.login(cfg['user'], cfg['password'])
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"  ✗ SMTP error: {e}")
        return False


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_draft(db, supplier_ids=None):
    """Generate drafts for suppliers that don't have one yet."""
    suppliers = get_pending_suppliers(db, supplier_ids)

    if not suppliers:
        if supplier_ids:
            print(f"No new drafts needed for IDs {supplier_ids} (may already have drafts)")
        else:
            print("No suppliers need drafting — all have existing drafts.")
        return

    print(f"📝 Drafting {len(suppliers)} messages...\n")

    for supplier in suppliers:
        sid = supplier['id']
        name = supplier['trade_name']
        country = detect_country(supplier.get('factory_locations', ''))
        supplier['_country'] = country
        strategy = get_strategy(country)
        channel = strategy['primary']
        language = strategy['language']

        if channel == 'wechat' and language == 'zh':
            content = generate_wechat_message(supplier)
            subject = None
        else:
            content = generate_email_message(supplier)
            subject = generate_email_subject(supplier)

        msg_id = log_draft(db, sid, channel, content, subject)

        has_email = "📧" if supplier.get('email') else "  "
        print(f"  ✓ ID {sid:>2}: {name:<32} [{country}] → {channel} ({language})  {has_email}  draft #{msg_id}")

    print(f"\n{len(suppliers)} drafts created. Use 'review' to approve.")


def cmd_review(db):
    """Show all DRAFT messages for review."""
    drafts = get_drafts(db, 'DRAFT')

    if not drafts:
        print("No drafts pending review.")
        approved = get_drafts(db, 'APPROVED')
        if approved:
            print(f"\n{len(approved)} messages APPROVED and ready to send.")
        return

    print(f"📋 {len(drafts)} DRAFT messages pending review:\n")

    for d in drafts:
        country = detect_country(d.get('factory_locations', ''))
        has_email = "📧" if d.get('email') else "⚠ NO EMAIL"
        channel_icon = "💬" if d['channel'] == 'wechat' else "📧"

        print(f"{'─'*60}")
        print(f"  #{d['id']:>3}  {channel_icon} {d['trade_name']:<30} [{country}]  {has_email}")
        print(f"       Supplier ID: {d['supplier_id']}  |  Channel: {d['channel']}")

        # Extract subject if email
        if d['channel'] == 'email':
            subject_match = re.match(r'\[Subject: (.*?)\]', d['content'])
            if subject_match:
                print(f"       Subject: {subject_match.group(1)}")

        # Show message preview
        preview = d['content'][:200].replace('\n', ' ↵ ')
        print(f"       Preview: {preview}...")

    print(f"\n{'─'*60}")
    print(f"To approve:  python outreach_agent.py approve --msg <ids>")
    print(f"To approve by supplier:  python outreach_agent.py approve --supplier <ids>")
    print(f"To reject:   python outreach_agent.py reject --msg <ids>")
    print(f"To send all approved:     python outreach_agent.py send")


def cmd_approve(db, msg_ids=None, supplier_ids=None, approved_by="user"):
    """Approve draft messages."""
    updated = 0

    if msg_ids:
        updated = update_approval(db, msg_ids, 'APPROVED', approved_by)
        print(f"✓ Approved {updated} message(s) by msg ID: {msg_ids}")

    if supplier_ids:
        drafts = get_drafts_by_supplier(db, supplier_ids, 'DRAFT')
        if drafts:
            ids = [d['id'] for d in drafts]
            updated += update_approval(db, ids, 'APPROVED', approved_by)
            print(f"✓ Approved {len(ids)} message(s) by supplier ID: {supplier_ids}")
        else:
            print(f"No DRAFT messages found for supplier IDs: {supplier_ids}")

    if not msg_ids and not supplier_ids:
        # Approve all drafts
        drafts = get_drafts(db, 'DRAFT')
        if drafts:
            ids = [d['id'] for d in drafts]
            updated = update_approval(db, ids, 'APPROVED', approved_by)
            print(f"✓ Approved ALL {updated} draft messages")
        else:
            print("No drafts to approve.")


def cmd_reject(db, msg_ids=None):
    """Reject draft messages."""
    if not msg_ids:
        print("Specify messages to reject: --msg <ids>")
        return
    updated = update_approval(db, msg_ids, 'REJECTED')
    print(f"✗ Rejected {updated} message(s)")


def cmd_send(db):
    """Send all APPROVED messages."""
    drafts = get_drafts(db, 'APPROVED')

    if not drafts:
        print("No approved messages to send.")
        return

    print(f"📤 Sending {len(drafts)} approved messages...\n")

    sent_count = 0
    wechat_pending = 0

    for d in drafts:
        sid = d['supplier_id']
        name = d['trade_name']
        channel = d['channel']

        if channel == 'email':
            to_addr = d.get('email', '')
            if not to_addr:
                print(f"  ✗ ID {sid}: {name} — NO EMAIL ADDRESS")
                continue

            # Extract subject from content
            subject_match = re.match(r'\[Subject: (.*?)\]\n\n', d['content'])
            subject = subject_match.group(1) if subject_match else f"Hotel Towel Manufacturing — {name}"
            body = re.sub(r'\[Subject:.*?\]\n\n', '', d['content'], count=1)

            success = send_email(to_addr, subject, body)
            if success:
                cur = db.cursor()
                cur.execute("""
                    UPDATE conversation_log SET approval_status = 'SENT', agent_action = 'SENT' WHERE id = ?
                """, (d['id'],))
                db.commit()
                update_supplier_state(db, sid, 'OUTREACH_SENT', 'email')
                print(f"  ✓ ID {sid}: {name} → {to_addr}")
                sent_count += 1
            else:
                print(f"  ✗ ID {sid}: {name} — send failed")

        elif channel == 'wechat':
            # WeChat requires manual send or Official Account API
            print(f"  💬 ID {sid}: {name} — WeChat draft ready for manual send")
            wechat_pending += 1

    print(f"\n📊 Sent: {sent_count} email(s) | WeChat pending: {wechat_pending}")


def cmd_status(db):
    """Show pipeline overview."""
    cur = db.cursor()

    # State counts
    cur.execute("SELECT outreach_state, COUNT(*) as cnt FROM suppliers GROUP BY outreach_state ORDER BY cnt DESC")
    print("📊 Pipeline Status")
    print("=" * 55)
    for row in cur.fetchall():
        bar = "█" * row['cnt']
        print(f"  {row['outreach_state']:<20} {row['cnt']:>3}  {bar}")

    # Approval queue
    cur.execute("SELECT approval_status, COUNT(*) as cnt FROM conversation_log GROUP BY approval_status")
    print(f"\n📧 Message Queue:")
    for row in cur.fetchall():
        icon = {"DRAFT": "📝", "APPROVED": "✅", "SENT": "📤", "REJECTED": "❌"}.get(row['approval_status'], "?")
        print(f"  {icon} {row['approval_status']:<12} {row['cnt']:>3}")

    # Email coverage
    cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) as has_email FROM suppliers")
    r = cur.fetchone()
    print(f"\n📧 Email coverage: {r['has_email']}/{r['total']} suppliers")

    # Next targets
    cur.execute("""
        SELECT s.id, s.trade_name, s.factory_locations, s.qualification_score, s.email
        FROM suppliers s
        LEFT JOIN conversation_log cl ON s.id = cl.supplier_id AND cl.agent_action = 'DRAFT'
        WHERE s.outreach_state IN ('ENRICHED', 'DISCOVERED')
        AND cl.id IS NULL
        ORDER BY s.qualification_score DESC
    """)
    pending = cur.fetchall()

    if pending:
        print(f"\n🎯 Suppliers without drafts ({len(pending)}):")
        for s in pending[:8]:
            c = detect_country(s['factory_locations'])
            ch = get_strategy(c)['primary']
            has_email = "📧" if s['email'] else "—"
            print(f"  {s['id']:>2}. {s['trade_name']:<32} [{c}] → {ch}  {has_email}")
        if len(pending) > 8:
            print(f"  ... and {len(pending)-8} more")


def cmd_full_pipeline(db, supplier_ids=None):
    """Draft + present for review in one step."""
    cmd_draft(db, supplier_ids)
    print()
    cmd_review(db)


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Atelier Supplier Outreach Agent")
    sub = parser.add_subparsers(dest="command", help="Command")

    # draft
    p_draft = sub.add_parser("draft", help="Generate drafts for pending suppliers")
    p_draft.add_argument("--id", type=str, help="Comma-separated supplier IDs")

    # review
    sub.add_parser("review", help="Show DRAFT messages for approval")

    # approve
    p_approve = sub.add_parser("approve", help="Approve draft messages")
    p_approve.add_argument("--msg", type=str, help="Comma-separated message IDs")
    p_approve.add_argument("--supplier", type=str, help="Comma-separated supplier IDs")
    p_approve.add_argument("--all", action="store_true", help="Approve all drafts")

    # reject
    p_reject = sub.add_parser("reject", help="Reject draft messages")
    p_reject.add_argument("--msg", type=str, help="Comma-separated message IDs")

    # send
    sub.add_parser("send", help="Send all APPROVED messages")

    # status
    sub.add_parser("status", help="Pipeline overview")

    # full-pipeline
    p_full = sub.add_parser("full-pipeline", help="Draft + review")
    p_full.add_argument("--id", type=str, help="Comma-separated supplier IDs")

    args = parser.parse_args()
    db = get_db()

    def parse_ids(s):
        if not s:
            return None
        return [int(x.strip()) for x in s.split(',')]

    if args.command == "draft":
        cmd_draft(db, parse_ids(getattr(args, 'id', None)))
    elif args.command == "review":
        cmd_review(db)
    elif args.command == "approve":
        msg_ids = parse_ids(getattr(args, 'msg', None))
        supplier_ids = parse_ids(getattr(args, 'supplier', None))
        if getattr(args, 'all', False):
            cmd_approve(db)
        else:
            cmd_approve(db, msg_ids, supplier_ids)
    elif args.command == "reject":
        msg_ids = parse_ids(getattr(args, 'msg', None))
        cmd_reject(db, msg_ids)
    elif args.command == "send":
        cmd_send(db)
    elif args.command == "status":
        cmd_status(db)
    elif args.command == "full-pipeline":
        cmd_full_pipeline(db, parse_ids(getattr(args, 'id', None)))
    else:
        parser.print_help()

    db.close()
