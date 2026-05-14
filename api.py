"""
Atelier Agentic Growth System — FastAPI Backend
Endpoints: briefs, suppliers, discovery, enrichment, outreach
"""
import os, json, re, sqlite3, asyncio, logging
from datetime import datetime
from typing import Optional
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("atelier-api")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "suppliers.db"))
DEPLOY_DIR = os.environ.get("DEPLOY_DIR", os.path.join(os.path.dirname(__file__), "deploy"))

app = FastAPI(title="Atelier Agentic Growth System", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Debug endpoint ──────────────────────────────────────────
@app.get("/api/debug/deps")
def debug_deps():
    """Check if search dependencies are installed."""
    deps = {}
    for mod_name in ['ddgs', 'duckduckgo_search', 'bs4', 'requests']:
        try:
            m = __import__(mod_name)
            deps[mod_name] = {'installed': True, 'version': getattr(m, '__version__', 'unknown')}
        except ImportError as e:
            deps[mod_name] = {'installed': False, 'error': str(e)}
    # Try a quick search
    try:
        from ddgs import DDGS
        with DDGS() as d:
            r = list(d.text('eyewear manufacturer', max_results=2))
        deps['search_test'] = {'works': True, 'results': len(r)}
    except Exception as e:
        try:
            from duckduckgo_search import DDGS as DDGS2
            with DDGS2() as d:
                r = list(d.text('eyewear manufacturer', max_results=2))
            deps['search_test'] = {'works': True, 'results': len(r), 'via': 'duckduckgo_search'}
        except Exception as e2:
            deps['search_test'] = {'works': False, 'error': str(e), 'fallback_error': str(e2)}
    return deps

@app.get("/api/debug/discover/{brief_id}")
def debug_discover(brief_id: int):
    """Run discovery inline and return the error if it fails."""
    try:
        from discovery_agent import discover_suppliers
        with get_db() as db:
            row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
            if not row:
                return {"error": "Brief not found"}
            brief = dict(row)
        results = discover_suppliers(brief)
        return {"success": True, "count": len(results), "suppliers": [s.get("trade_name","?") for s in results[:5]]}
    except Exception as e:
        import traceback
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}

# ─── DB helpers ───────────────────────────────────────────────
@contextmanager
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    finally:
        db.close()

def dict_from_row(row):
    if row is None:
        return None
    return dict(row)

def dict_list_from_rows(rows):
    return [dict(r) for r in rows]

# ─── DB Health ───────────────────────────────────────────────
@app.get("/api/health")
def health():
    db_info = {"db_path": DB_PATH, "db_exists": os.path.exists(DB_PATH)}
    try:
        with get_db() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            counts = {}
            for t in tables:
                try:
                    counts[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except:
                    counts[t] = "error"
            db_info["tables"] = counts
    except Exception as e:
        db_info["error"] = str(e)
    return {"status": "ok", "db": db_info}

# ─── Models ───────────────────────────────────────────────────
class BriefCreate(BaseModel):
    product_name: str
    brand: Optional[str] = ""
    customer: Optional[str] = ""
    priority: Optional[str] = "Medium"
    description: Optional[str] = ""
    formulation_type: Optional[str] = ""
    country_of_origin: Optional[str] = ""
    country_stipulations: Optional[str] = ""
    distribution_channels: Optional[str] = ""
    delivery_location: Optional[str] = ""
    market_position: Optional[str] = ""
    key_ingredients: Optional[str] = ""
    certifications_required: Optional[str] = ""
    registrations_required: Optional[str] = ""
    manufacturer_stipulations: Optional[str] = ""
    documentation_required: Optional[str] = ""
    usp: Optional[str] = ""
    packaging_type: Optional[str] = ""
    label_type: Optional[str] = ""
    target_regions: Optional[str] = ""
    annual_volume: Optional[str] = ""
    price_point: Optional[str] = ""
    sku_count: Optional[str] = ""
    category: Optional[str] = ""
    date_quote_due: Optional[str] = ""
    date_delivery_dc: Optional[str] = ""
    date_sample_due: Optional[str] = ""
    date_pp_sample: Optional[str] = ""
    date_formulation_approval: Optional[str] = ""
    date_packaging_approval: Optional[str] = ""
    date_po: Optional[str] = ""
    date_dispatch: Optional[str] = ""
    owner: Optional[str] = "hudson@atelier.co"

class BriefUpdate(BaseModel):
    product_name: Optional[str] = None
    brand: Optional[str] = None
    customer: Optional[str] = None
    priority: Optional[str] = None
    description: Optional[str] = None
    formulation_type: Optional[str] = None
    country_of_origin: Optional[str] = None
    country_stipulations: Optional[str] = None
    distribution_channels: Optional[str] = None
    status: Optional[str] = None

class ApprovalAction(BaseModel):
    action: str  # "approve" or "reject"
    message_ids: Optional[list[int]] = None
    supplier_ids: Optional[list[int]] = None

# ─── Brief CRUD ──────────────────────────────────────────────
@app.get("/api/briefs")
def list_briefs():
    with get_db() as db:
        rows = db.execute("SELECT * FROM briefs ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            d = dict_from_row(row)
            # Add supplier count
            count = db.execute("SELECT COUNT(*) as c FROM brief_suppliers WHERE brief_id=?", [d["id"]]).fetchone()[0]
            d["_supplier_count"] = count
            result.append(d)
        return result

@app.post("/api/briefs")
def create_brief(brief: BriefCreate, background_tasks: BackgroundTasks):
    with get_db() as db:
        cols = [k for k, v in brief.model_dump().items()]
        vals = [v for k, v in brief.model_dump().items()]
        placeholders = ",".join(["?"] * len(cols))
        col_names = ",".join(cols)
        cur = db.execute(f"INSERT INTO briefs ({col_names}) VALUES ({placeholders})", vals)
        brief_id = cur.lastrowid
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        # Auto-trigger discovery for all new briefs
        background_tasks.add_task(run_discovery, brief_id)
        return dict_from_row(row)

@app.get("/api/briefs/{brief_id}")
def get_brief(brief_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        if not row:
            raise HTTPException(404, "Brief not found")
        d = dict_from_row(row)
        count = db.execute("SELECT COUNT(*) as c FROM brief_suppliers WHERE brief_id=?", [brief_id]).fetchone()[0]
        d["_supplier_count"] = count
        return d

@app.patch("/api/briefs/{brief_id}")
def update_brief(brief_id: int, brief: BriefUpdate):
    with get_db() as db:
        updates = {k: v for k, v in brief.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(400, "No fields to update")
        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ",".join([f"{k}=?" for k in updates])
        vals = list(updates.values()) + [brief_id]
        db.execute(f"UPDATE briefs SET {set_clause} WHERE id=?", vals)
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        return dict_from_row(row)

@app.get("/api/briefs/{brief_id}/suppliers")
def get_brief_suppliers(brief_id: int):
    with get_db() as db:
        rows = db.execute("""
            SELECT s.*, bs.match_score, bs.discovered_at
            FROM brief_suppliers bs
            JOIN suppliers s ON s.id = bs.supplier_id
            WHERE bs.brief_id=?
            ORDER BY bs.match_score DESC
        """, [brief_id]).fetchall()
        return dict_list_from_rows(rows)

# ─── Suppliers ────────────────────────────────────────────────
@app.get("/api/suppliers")
def list_suppliers(brief_id: Optional[int] = None):
    with get_db() as db:
        if brief_id:
            rows = db.execute("""
                SELECT s.*, bs.match_score FROM suppliers s
                JOIN brief_suppliers bs ON s.id = bs.supplier_id
                WHERE bs.brief_id=? ORDER BY bs.match_score DESC
            """, [brief_id]).fetchall()
        else:
            rows = db.execute("SELECT * FROM suppliers ORDER BY id").fetchall()
        return dict_list_from_rows(rows)

@app.get("/api/suppliers/{supplier_id}")
def get_supplier(supplier_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM suppliers WHERE id=?", [supplier_id]).fetchone()
        if not row:
            raise HTTPException(404, "Supplier not found")
        return dict_from_row(row)

@app.patch("/api/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, updates: dict):
    with get_db() as db:
        if not updates:
            raise HTTPException(400, "No fields to update")
        set_clause = ",".join([f"{k}=?" for k in updates.keys()])
        vals = list(updates.values()) + [supplier_id]
        db.execute(f"UPDATE suppliers SET {set_clause} WHERE id=?", vals)
        row = db.execute("SELECT * FROM suppliers WHERE id=?", [supplier_id]).fetchone()
        return dict_from_row(row)

# ─── Research (agent-triggered) ──────────────────────────────
@app.post("/api/briefs/{brief_id}/discover")
def trigger_discovery(brief_id: int, background_tasks: BackgroundTasks):
    with get_db() as db:
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        if not row:
            raise HTTPException(404, "Brief not found")
    # Launch research agent as a subprocess (avoids Railway background task timeout)
    background_tasks.add_task(run_research_agent, brief_id)
    return {"status": "research_started", "brief_id": brief_id}

@app.post("/api/briefs/{brief_id}/enrich")
def trigger_enrichment(brief_id: int, background_tasks: BackgroundTasks):
    """Enrichment is now handled by the research agent. This endpoint kept for API compat."""
    with get_db() as db:
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        if not row:
            raise HTTPException(404, "Brief not found")
    background_tasks.add_task(run_research_agent, brief_id)
    return {"status": "research_started", "brief_id": brief_id}

def run_research_agent(brief_id: int):
    """Launch research_agent.py as a subprocess — avoids FastAPI background task timeouts."""
    import subprocess
    logger.info(f"Launching research agent subprocess for brief {brief_id}")
    try:
        # Set OPENROUTER_API_KEY in the subprocess env
        env = os.environ.copy()
        # Try to load from .hermes/.env if not already set
        if not env.get("OPENROUTER_API_KEY"):
            dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".hermes", ".env")
            if os.path.exists(dotenv_path):
                with open(dotenv_path) as f:
                    for line in f:
                        if line.strip().startswith("OPENROUTER_API_KEY="):
                            env["OPENROUTER_API_KEY"] = line.strip().split("=", 1)[1]
                            break

        with get_db() as db:
            db.execute("UPDATE briefs SET status='RESEARCHING', updated_at=? WHERE id=?",
                      [datetime.utcnow().isoformat(), brief_id])

        script_path = os.path.join(os.path.dirname(__file__), "research_agent.py")
        result = subprocess.run(
            ["python3", script_path, "--brief-id", str(brief_id), "--max-suppliers", "20"],
            capture_output=True, text=True, timeout=600, env=env,
            cwd=os.path.dirname(__file__)
        )
        logger.info(f"Research agent finished for brief {brief_id}: exit={result.returncode}")
        if result.stdout:
            logger.info(f"Research agent stdout: {result.stdout[:500]}")
        if result.stderr:
            logger.warning(f"Research agent stderr: {result.stderr[:500]}")
        if result.returncode != 0:
            with get_db() as db:
                db.execute("UPDATE briefs SET status='RESEARCH_FAILED', updated_at=? WHERE id=?",
                          [datetime.utcnow().isoformat(), brief_id])
        else:
            # Auto-chain: research → outreach drafting
            logger.info(f"Research agent succeeded for brief {brief_id}, auto-chaining outreach")
            run_outreach_drafts(brief_id)
    except subprocess.TimeoutExpired:
        logger.error(f"Research agent timed out for brief {brief_id}")
        try:
            with get_db() as db:
                db.execute("UPDATE briefs SET status='RESEARCH_FAILED', updated_at=? WHERE id=?",
                          [datetime.utcnow().isoformat(), brief_id])
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Research agent failed for brief {brief_id}: {e}")
        try:
            with get_db() as db:
                db.execute("UPDATE briefs SET status='RESEARCH_FAILED', updated_at=? WHERE id=?",
                          [datetime.utcnow().isoformat(), brief_id])
        except Exception:
            pass

def run_enrichment(brief_id: int):
    """Background task: enrich discovered suppliers with detailed data.

    Processes suppliers in small batches to avoid timeout.
    Commits each batch so progress is preserved even if later batches fail.
    """
    logger.info(f"Enrichment started for brief {brief_id}")
    try:
        from enrich_agent import enrich_suppliers
        logger.info(f"Enrichment: enrich_agent imported OK for brief {brief_id}")
        # Get valid column names from DB schema
        with get_db() as db:
            schema_cols = {r[1] for r in db.execute("PRAGMA table_info(suppliers)").fetchall()}
            suppliers = db.execute("""
                SELECT s.* FROM suppliers s
                JOIN brief_suppliers bs ON s.id = bs.supplier_id
                WHERE bs.brief_id=? AND s.outreach_state='DISCOVERED'
            """, [brief_id]).fetchall()
            supplier_dicts = dict_list_from_rows(suppliers)
        logger.info(f"Enrichment: found {len(supplier_dicts)} DISCOVERED suppliers for brief {brief_id}")
        if not supplier_dicts:
            logger.warning(f"Enrichment: no DISCOVERED suppliers found for brief {brief_id}, skipping")
            return

        total_enriched = 0
        batch_size = 5  # Process 5 at a time to avoid timeout
        for i in range(0, len(supplier_dicts), batch_size):
            batch = supplier_dicts[i:i + batch_size]
            logger.info(f"Enrichment: processing batch {i//batch_size + 1} ({len(batch)} suppliers) for brief {brief_id}")
            enriched = enrich_suppliers(batch)
            # Write batch to DB immediately
            with get_db() as db:
                for s in enriched:
                    updates = {k: v for k, v in s.items() if k != "id" and v and k in schema_cols}
                    if updates and "id" in s:
                        set_clause = ",".join([f"{k}=?" for k in updates.keys()])
                        vals = list(updates.values()) + [s["id"]]
                        db.execute(f"UPDATE suppliers SET {set_clause} WHERE id=?", vals)
                db.execute("UPDATE briefs SET status='ENRICHING', updated_at=? WHERE id=?",
                          [datetime.utcnow().isoformat(), brief_id])
            total_enriched += len(enriched)
            logger.info(f"Enrichment: batch complete, {total_enriched} total enriched for brief {brief_id}")

        # Final status update
        with get_db() as db:
            db.execute("UPDATE briefs SET status='ENRICHED', updated_at=? WHERE id=?",
                      [datetime.utcnow().isoformat(), brief_id])
        logger.info(f"Enrichment complete for brief {brief_id}: enriched {total_enriched} suppliers")
        # Auto-chain: enrichment → outreach drafting
        logger.info(f"Auto-chaining outreach drafting for brief {brief_id}")
        run_outreach_drafts(brief_id)
    except Exception as e:
        import traceback
        logger.error(f"Enrichment failed for brief {brief_id}: {e}\n{traceback.format_exc()}")
        try:
            with get_db() as db:
                db.execute("UPDATE briefs SET status='ENRICHMENT_FAILED', updated_at=? WHERE id=?",
                          [datetime.utcnow().isoformat(), brief_id])
        except Exception:
            pass

# ─── Outreach ─────────────────────────────────────────────────
@app.post("/api/briefs/{brief_id}/draft-outreach")
def trigger_outreach_drafts(brief_id: int, background_tasks: BackgroundTasks):
    with get_db() as db:
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        if not row:
            raise HTTPException(404, "Brief not found")
    background_tasks.add_task(run_outreach_drafts, brief_id)
    return {"status": "outreach_drafting", "brief_id": brief_id}

def run_outreach_drafts(brief_id: int):
    """Generate personalized outreach drafts for all enriched suppliers in a brief."""
    logger.info(f"Outreach drafting started for brief {brief_id}")
    try:
        with get_db() as db:
            suppliers = db.execute("""
                SELECT s.* FROM suppliers s
                JOIN brief_suppliers bs ON s.id = bs.supplier_id
                WHERE bs.brief_id=? AND s.outreach_state IN ('ENRICHED','DISCOVERED')
            """, [brief_id]).fetchall()
            brief = dict(db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone())

        from outreach_agent import generate_wechat_message, generate_email_message, generate_email_subject, detect_country, get_strategy, log_draft, update_supplier_state
        drafted = 0
        for s in suppliers:
            supplier = dict(s)
            country = detect_country(supplier.get("factory_locations", ""))
            strategy = get_strategy(country)
            channel = strategy["primary"]
            if channel == "wechat":
                content = generate_wechat_message(supplier, brief)
                subject = None
            else:
                content = generate_email_message(supplier, brief)
                subject = generate_email_subject(supplier, brief)
            with get_db() as db:
                log_draft(db, supplier["id"], channel, content, subject)
                update_supplier_state(db, supplier["id"], "ENRICHED", channel)
            drafted += 1

        with get_db() as db:
            db.execute("UPDATE briefs SET status='OUTREACH_DRAFTED', updated_at=? WHERE id=?",
                      [datetime.utcnow().isoformat(), brief_id])
        logger.info(f"Outreach drafting complete for brief {brief_id}: drafted {drafted} messages")
    except Exception as e:
        logger.error(f"Outreach drafting failed for brief {brief_id}: {e}")

# ─── Full Pipeline ─────────────────────────────────────────────
@app.post("/api/briefs/{brief_id}/run-pipeline")
def run_full_pipeline(brief_id: int, background_tasks: BackgroundTasks):
    with get_db() as db:
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        if not row:
            raise HTTPException(404, "Brief not found")
    background_tasks.add_task(run_pipeline, brief_id)
    return {"status": "pipeline_started", "brief_id": brief_id}

def run_pipeline(brief_id: int):
    """Run the full pipeline: discover → enrich → draft outreach."""
    run_discovery(brief_id)
    run_enrichment(brief_id)
    run_outreach_drafts(brief_id)

# ─── Conversations / Approval ─────────────────────────────────
@app.get("/api/conversations")
def get_conversations(supplier_id: Optional[int] = None):
    with get_db() as db:
        if supplier_id:
            rows = db.execute("SELECT * FROM conversation_log WHERE supplier_id=? ORDER BY timestamp", [supplier_id]).fetchall()
        else:
            rows = db.execute("SELECT * FROM conversation_log ORDER BY timestamp DESC LIMIT 100").fetchall()
        return dict_list_from_rows(rows)

@app.post("/api/approve")
def approve_messages(action: ApprovalAction):
    with get_db() as db:
        count = 0
        if action.message_ids:
            for mid in action.message_ids:
                status = "APPROVED" if action.action == "approve" else "REJECTED"
                db.execute("UPDATE conversation_log SET approval_status=? WHERE id=?", [status, mid])
                count += 1
        elif action.supplier_ids:
            for sid in action.supplier_ids:
                status = "APPROVED" if action.action == "approve" else "REJECTED"
                db.execute("UPDATE conversation_log SET approval_status=? WHERE supplier_id=? AND approval_status='DRAFT'", [status, sid])
                count += 1
        return {"action": action.action, "updated": count}

# ─── Dashboard rebuild ────────────────────────────────────────
@app.post("/api/rebuild-dashboard")
def rebuild_dashboard(background_tasks: BackgroundTasks):
    background_tasks.add_task(rebuild_dashboard_html)
    return {"status": "rebuild_started"}

def rebuild_dashboard_html():
    """Regenerate the static dashboard HTML from current DB data."""
    import subprocess
    script = "/data/luxury_towel_suppliers/build_dashboard.py"
    try:
        result = subprocess.run(["python3", script], capture_output=True, text=True, timeout=60)
        logger.info(f"Dashboard rebuild: {result.stdout[-200:] if result.stdout else 'ok'}")
        if result.returncode != 0:
            logger.error(f"Dashboard rebuild failed: {result.stderr[-500:]}")
    except Exception as e:
        logger.error(f"Dashboard rebuild error: {e}")

# ─── Status / Health ──────────────────────────────────────────
@app.get("/api/status")
def get_status():
    with get_db() as db:
        briefs_count = db.execute("SELECT COUNT(*) as c FROM briefs").fetchone()["c"]
        suppliers_count = db.execute("SELECT COUNT(*) as c FROM suppliers").fetchone()["c"]
        drafts = db.execute("SELECT COUNT(*) as c FROM conversation_log WHERE approval_status='DRAFT'").fetchone()["c"]
        approved = db.execute("SELECT COUNT(*) as c FROM conversation_log WHERE approval_status='APPROVED'").fetchone()["c"]
        sent = db.execute("SELECT COUNT(*) as c FROM conversation_log WHERE approval_status='SENT'").fetchone()["c"]
    return {
        "briefs": briefs_count,
        "suppliers": suppliers_count,
        "drafts": drafts,
        "approved": approved,
        "sent": sent,
        "status": "running"
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/debug/last-errors")
def last_errors():
    """Return last 20 log entries from the in-memory handler."""
    from logging.handlers import MemoryHandler
    # Just return recent DB state for debugging
    with get_db() as db:
        briefs = db.execute("SELECT id, product_name, status FROM briefs ORDER BY id DESC LIMIT 10").fetchall()
        recent_suppliers = db.execute("SELECT id, trade_name, outreach_state, qualification_score, data_completeness_score FROM suppliers ORDER BY id DESC LIMIT 10").fetchall()
        links = db.execute("SELECT brief_id, supplier_id, match_score FROM brief_suppliers ORDER BY rowid DESC LIMIT 20").fetchall()
    return {
        "briefs": [dict(r) for r in briefs],
        "recent_suppliers": [dict(r) for r in recent_suppliers],
        "recent_links": [dict(r) for r in links],
    }

@app.get("/api/debug/enrich/{brief_id}")
def debug_enrich(brief_id: int):
    """Synchronous enrichment for debugging — returns result or error directly."""
    try:
        from enrich_agent import enrich_suppliers
        with get_db() as db:
            schema_cols = {r[1] for r in db.execute("PRAGMA table_info(suppliers)").fetchall()}
            suppliers = db.execute("""
                SELECT s.* FROM suppliers s
                JOIN brief_suppliers bs ON s.id = bs.supplier_id
                WHERE bs.brief_id=? AND s.outreach_state='DISCOVERED'
            """, [brief_id]).fetchall()
            supplier_dicts = dict_list_from_rows(suppliers)
        if not supplier_dicts:
            return {"error": "No DISCOVERED suppliers found", "brief_id": brief_id}
        # Only enrich first 2 for speed
        enriched = enrich_suppliers(supplier_dicts[:2])
        results = []
        for s in enriched:
            results.append({k: s.get(k) for k in ["trade_name","legal_name","email","contact_name","moq","qualification_score","data_completeness_score","outreach_state"]})
        return {"success": True, "count": len(enriched), "results": results}
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}

# ─── Static files (dashboard) ─────────────────────────────────
@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(DEPLOY_DIR, "index.html"))

# ─── Run ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
