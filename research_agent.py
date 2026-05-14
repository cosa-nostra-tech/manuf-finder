"""
Research Agent — Adaptive supplier research via LLM-guided search + scraping.

Instead of fixed regex pipelines, this agent uses an LLM to make adaptive
decisions about what to search for, which results are real suppliers, and
how to extract structured data — exactly like a human researcher would.

Architecture:
  - ReAct loop: LLM decides action → tool executes → result fed back → repeat
  - Tools: web_search, scrape_page, save_supplier
  - Uses OpenRouter API for LLM calls
  - Writes results directly to the shared SQLite DB

Usage:
  python research_agent.py --brief-id 11
  python research_agent.py --brief-id 11 --max-suppliers 10
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

# ─── Config ────────────────────────────────────────────────────────────────

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
STRATEGY_MODEL = os.environ.get("RESEARCH_STRATEGY_MODEL", "anthropic/claude-sonnet-4")
EXTRACT_MODEL = os.environ.get("RESEARCH_EXTRACT_MODEL", "openai/gpt-4o-mini")
MAX_ITERATIONS = int(os.environ.get("RESEARCH_MAX_ITERATIONS", "60"))
MAX_SUPPLIERS = int(os.environ.get("RESEARCH_MAX_SUPPLIERS", "20"))

# DB path — same as api.py: use relative path by default
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "suppliers.db"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [research_agent] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ─── LLM Client ────────────────────────────────────────────────────────────

def call_llm(
    messages: list[dict],
    model: str = STRATEGY_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> str:
    """Call OpenRouter API and return the assistant message content."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set — cannot make LLM calls")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://manuf-finder-production.up.railway.app",
        "X-Title": "Atelier Research Agent",
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    for attempt in range(3):
        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            logger.warning(f"LLM API error (attempt {attempt+1}): {e.response.status_code} {e.response.text[:200]}")
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            else:
                raise
        except Exception as e:
            logger.warning(f"LLM API error (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            else:
                raise
    raise RuntimeError("LLM API failed after 3 attempts")


# ─── Tools ─────────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 10) -> list[dict]:
    """Search DuckDuckGo and return results."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("link", "")),
                    "snippet": r.get("body", ""),
                }
                for r in results
            ]
    except Exception as e:
        logger.warning(f"Search error for '{query}': {e}")
        return []


def scrape_page(url: str, max_chars: int = 8000) -> str:
    """Scrape a URL and return cleaned text content."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove nav, footer, script, style noise
        for tag in soup.find_all(["script", "style", "nav", "noscript", "iframe"]):
            tag.decompose()

        # Try to get meaningful content areas
        content_parts = []

        # Title
        if soup.title and soup.title.string:
            content_parts.append(f"PAGE TITLE: {soup.title.string.strip()}")

        # H1 headings
        for h1 in soup.find_all("h1", limit=3):
            content_parts.append(f"H1: {h1.get_text(strip=True)}")

        # Footer (often has legal name, contact)
        footer = soup.find("footer")
        if footer:
            footer_text = footer.get_text(separator=" ", strip=True)
            if footer_text:
                content_parts.append(f"FOOTER: {footer_text[:500]}")

        # About section
        for el in soup.find_all(string=re.compile(r"about us", re.I)):
            parent = el.find_parent(["div", "section", "article"])
            if parent:
                content_parts.append(f"ABOUT SECTION: {parent.get_text(separator=' ', strip=True)[:1000]}")
                break

        # Contact section
        for el in soup.find_all(string=re.compile(r"contact", re.I)):
            parent = el.find_parent(["div", "section", "article"])
            if parent:
                content_parts.append(f"CONTACT SECTION: {parent.get_text(separator=' ', strip=True)[:800]}")
                break

        # All text as fallback
        full_text = soup.get_text(separator=" ", strip=True)
        content_parts.append(f"FULL PAGE TEXT: {full_text}")

        combined = "\n\n".join(content_parts)
        return combined[:max_chars]

    except Exception as e:
        logger.warning(f"Scrape error for {url}: {e}")
        return f"ERROR: Could not scrape {url}: {e}"


# ─── DB Helpers ────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_brief(brief_id: int) -> Optional[dict]:
    with get_db() as db:
        row = db.execute("SELECT * FROM briefs WHERE id=?", [brief_id]).fetchone()
        return dict(row) if row else None


def get_schema_columns() -> set[str]:
    with get_db() as db:
        return {r[1] for r in db.execute("PRAGMA table_info(suppliers)").fetchall()}


def supplier_exists(trade_name: str, website: str = "") -> Optional[int]:
    """Check if a supplier already exists by trade name or website."""
    with get_db() as db:
        if website:
            row = db.execute(
                "SELECT id FROM suppliers WHERE website=? LIMIT 1",
                [website]
            ).fetchone()
            if row:
                return row["id"]
        row = db.execute(
            "SELECT id FROM suppliers WHERE trade_name=? LIMIT 1",
            [trade_name]
        ).fetchone()
        if row:
            return row["id"]
    return None


def save_supplier(supplier_data: dict, brief_id: int) -> int:
    """Insert or update a supplier and link to brief. Returns supplier ID."""
    schema_cols = get_schema_columns()

    # Check if supplier already exists
    existing_id = supplier_exists(
        supplier_data.get("trade_name", ""),
        supplier_data.get("website", "")
    )

    with get_db() as db:
        if existing_id:
            # Update existing supplier — only fill empty fields
            updates = {}
            for k, v in supplier_data.items():
                if k in schema_cols and v and str(v).strip():
                    existing = db.execute(
                        f"SELECT {k} FROM suppliers WHERE id=?", [existing_id]
                    ).fetchone()
                    existing_val = existing[k] if existing else None
                    if not existing_val or not str(existing_val).strip():
                        updates[k] = v
            if updates:
                set_clause = ",".join([f"{k}=?" for k in updates])
                vals = list(updates.values()) + [existing_id]
                db.execute(f"UPDATE suppliers SET {set_clause} WHERE id=?", vals)
            supplier_id = existing_id
        else:
            # Insert new supplier
            fields = {k: v for k, v in supplier_data.items() if k in schema_cols and v and str(v).strip()}
            if not fields.get("trade_name"):
                logger.warning(f"Skipping supplier with no trade_name: {supplier_data}")
                return -1
            fields["outreach_state"] = "ENRICHED"
            cols = ",".join(fields.keys())
            placeholders = ",".join(["?"] * len(fields))
            cursor = db.execute(
                f"INSERT INTO suppliers ({cols}) VALUES ({placeholders})",
                list(fields.values())
            )
            supplier_id = cursor.lastrowid

        # Link to brief (ignore if already linked)
        try:
            db.execute(
                "INSERT INTO brief_suppliers (brief_id, supplier_id, match_score) VALUES (?, ?, ?)",
                [brief_id, supplier_id, supplier_data.get("match_score", 80)]
            )
        except sqlite3.IntegrityError:
            pass  # Already linked

    logger.info(f"Saved supplier #{supplier_id}: {supplier_data.get('trade_name', 'unknown')}")
    return supplier_id


def update_brief_status(brief_id: int, status: str):
    with get_db() as db:
        db.execute(
            "UPDATE briefs SET status=?, updated_at=datetime('now') WHERE id=?",
            [status, brief_id]
        )


def get_existing_supplier_names() -> set[str]:
    """Get all existing supplier trade names to avoid duplicates."""
    with get_db() as db:
        rows = db.execute("SELECT trade_name FROM suppliers").fetchall()
        return {r["trade_name"].lower().strip() for r in rows if r["trade_name"]}


# ─── Agent System Prompts ──────────────────────────────────────────────────

STRATEGY_SYSTEM_PROMPT = """You are a supplier research agent for Atelier, a product development company. Your job is to find real manufacturers/suppliers for a given product brief.

You have these tools:
- search(query): Search DuckDuckGo. Returns list of {title, url, snippet}.
- scrape(url): Scrape a webpage and return its text content.
- save(supplier_data): Save a discovered supplier. supplier_data must be a JSON object with at least "trade_name". Prefer full data: trade_name, legal_name, website, email, contact_name, wechat_id, moq, factory_locations, supplier_type, product_categories, certs_and_audits, brands_worked_with, market_experience.

RESEARCH STRATEGY:
1. Start with 2-3 targeted search queries (e.g. "[product] manufacturer China OEM", "[product] supplier factory wholesale")
2. Evaluate search results — identify which are REAL suppliers vs blog articles/listicles/SEO spam
3. For each real supplier result, scrape their website to extract structured data
4. If a website is empty/minimal (JS-rendered), try cross-referencing on Alibaba: search "[company name] Alibaba" or "[company name] made-in-china"
5. Try at least 3 different search query angles to find diverse suppliers
6. Don't waste time on directory/listicle sites — skip them and search differently

QUALITY RULES:
- ONLY save REAL suppliers (companies that manufacture or supply the product)
- NEVER save blog articles, "Top 10" lists, directories, or informational sites
- If a search result title contains "Top", "Best", "Guide", "How to", "List of" — it's NOT a supplier
- Each supplier must have a real website URL
- Prefer suppliers with actual contact info (email, WeChat, phone)
- Aim for 15-20 suppliers per brief

Respond with ONE action per message in this format:
ACTION: search|scrape|save
PARAMS: <json or value>

For search: PARAMS: {"query": "your search query"}
For scrape: PARAMS: "https://example.com"
For save: PARAMS: {"trade_name": "...", "legal_name": "...", ...}
For done: ACTION: done
PARAMS: {"suppliers_found": N, "summary": "brief summary"}"""


EXTRACT_SYSTEM_PROMPT = """You are a data extraction specialist. Given the text content from a supplier's website, extract structured company information.

Return a JSON object with these fields (use null for anything not found):
- trade_name: The brand/trading name of the company
- legal_name: The registered legal entity name (e.g. "Shenzhen XYZ Eyewear Co., Ltd")
- website: The company website URL
- email: Contact email address
- contact_name: Name and title of a contact person (e.g. "Ms. Wang, Export Sales")
- wechat_id: WeChat ID if visible
- phone: Phone number
- moq: Minimum order quantity (e.g. "300 pcs", "1000 units")
- factory_locations: City/province of manufacturing (e.g. "Wenzhou, Zhejiang")
- supplier_type: "Manufacturer", "Trading Company", or "Manufacturer/Trading Company"
- product_categories: Main product categories (e.g. "Eyewear; Sunglasses; Optical Frames")
- product_sub_categories: Sub-categories (e.g. "Acetate; Titanium; Polarized")
- certs_and_audits: Certifications found (e.g. "ISO 9001; FDA; CE; BSCI")
- brands_worked_with: Brand names they've manufactured for (e.g. "Ralph Lauren; Disney; MUJI")
- market_experience: Years in business or markets served (e.g. "15 years, exports to 30+ countries")
- company_description: 1-2 sentence description of what they do
- founding_year: Year company was established

RULES:
- Only extract information that is CLEARLY stated on the page — do NOT guess or infer
- If a name looks like navigation text ("Home About Products Contact"), set it to null
- Legal names should contain a corporate suffix like "Co., Ltd", "Ltd", "Inc", "GmbH"
- Contact names should be a PERSON's name, not a department or generic term
- If the page is empty or just a login screen, return mostly null fields
- Return ONLY the JSON object, no other text"""


# ─── Agent Loop ────────────────────────────────────────────────────────────

def parse_action(response: str) -> tuple[str, str]:
    """Parse ACTION and PARAMS from LLM response."""
    action = "done"
    params = "{}"

    action_match = re.search(r"ACTION:\s*(\w+)", response, re.IGNORECASE)
    if action_match:
        action = action_match.group(1).lower().strip()

    params_match = re.search(r"PARAMS:\s*(.+?)(?:\n|$)", response, re.DOTALL | re.IGNORECASE)
    if params_match:
        params = params_match.group(1).strip()

    return action, params


def research_brief(brief_id: int, max_suppliers: int = MAX_SUPPLIERS):
    """Run the research agent for a single brief."""
    brief = get_brief(brief_id)
    if not brief:
        logger.error(f"Brief {brief_id} not found")
        return

    product = brief.get("product_name", "")
    category = brief.get("category", "")
    description = brief.get("description", "")
    country = brief.get("country_of_origin", "China")
    certs = brief.get("certifications_required", "")

    existing_names = get_existing_supplier_names()
    saved_count = 0

    # Brief context for the agent
    brief_context = f"""PRODUCT BRIEF:
- Product: {product}
- Category: {category}
- Description: {description}
- Country of Origin: {country}
- Required Certifications: {certs}

Existing suppliers already found (DO NOT duplicate these): {', '.join(sorted(existing_names)[:30])}
Target: Find {max_suppliers} real suppliers. Currently found: {saved_count}/{max_suppliers}"""

    messages = [
        {"role": "system", "content": STRATEGY_SYSTEM_PROMPT},
        {"role": "user", "content": brief_context},
    ]

    iteration = 0
    last_scrape_url = None

    logger.info(f"Starting research for brief {brief_id}: {product}")

    while iteration < MAX_ITERATIONS and saved_count < max_suppliers:
        iteration += 1
        logger.info(f"Iteration {iteration}/{MAX_ITERATIONS} | Suppliers: {saved_count}/{max_suppliers}")

        # Call strategy LLM
        response = call_llm(messages, model=STRATEGY_MODEL, temperature=0.3, max_tokens=1500)
        messages.append({"role": "assistant", "content": response})

        action, params = parse_action(response)
        logger.info(f"  Action: {action} | Params: {params[:100]}")

        if action == "done":
            logger.info(f"Agent finished: {params}")
            break

        elif action == "search":
            try:
                search_params = json.loads(params) if params.startswith("{") else {"query": params}
                query = search_params.get("query", search_params.get("q", ""))
                if not query:
                    messages.append({"role": "user", "content": "ERROR: No search query provided. Use format: {\"query\": \"your search\"}"})
                    continue

                results = web_search(query, max_results=10)
                if not results:
                    messages.append({"role": "user", "content": "No search results found. Try a different query."})
                else:
                    results_text = "\n".join(
                        f"{i+1}. {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}"
                        for i, r in enumerate(results)
                    )
                    messages.append({
                        "role": "user",
                        "content": f"Search results for '{query}':\n{results_text}\n\nDecide: which are REAL suppliers? Scrape the promising ones. Skip blog/listicle results."
                    })
                time.sleep(1.5)  # Rate limit DDG

            except json.JSONDecodeError:
                messages.append({"role": "user", "content": f"ERROR: Invalid JSON in params: {params}. Use format: {{\"query\": \"...\"}}"})

        elif action == "scrape":
            url = params.strip().strip('"').strip("'")
            if not url.startswith("http"):
                url = f"https://{url}"

            last_scrape_url = url
            logger.info(f"  Scraping: {url}")
            page_text = scrape_page(url)

            # Use extraction LLM to pull structured data
            extract_messages = [
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": f"URL: {url}\n\nPAGE CONTENT:\n{page_text}"},
            ]
            extracted_json = call_llm(extract_messages, model=EXTRACT_MODEL, temperature=0.1, max_tokens=1500)

            # Clean up the JSON response
            extracted_json = re.sub(r"```json\s*", "", extracted_json)
            extracted_json = re.sub(r"```\s*", "", extracted_json)
            extracted_json = extracted_json.strip()

            try:
                supplier_data = json.loads(extracted_json)
            except json.JSONDecodeError:
                # Try to find JSON in the response
                json_match = re.search(r"\{[^{}]*\}", extracted_json, re.DOTALL)
                if json_match:
                    try:
                        supplier_data = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        supplier_data = {}
                else:
                    supplier_data = {}

            if supplier_data and supplier_data.get("trade_name"):
                # Map common LLM field names to DB schema
                if "url" in supplier_data and "website" not in supplier_data:
                    supplier_data["website"] = supplier_data.pop("url")

                # Add website if not present
                if not supplier_data.get("website"):
                    supplier_data["website"] = url

                # Auto-detect category keywords
                supplier_data.setdefault("product_categories", category)
                supplier_data["match_score"] = 85

                # Compute scores
                _fields = {
                    "legal_name": supplier_data.get("legal_name"),
                    "email": supplier_data.get("email"),
                    "contact_name": supplier_data.get("contact_name"),
                    "wechat_id": supplier_data.get("wechat_id"),
                    "factory_locations": supplier_data.get("factory_locations"),
                    "certs_and_audits": supplier_data.get("certs_and_audits"),
                    "moq": supplier_data.get("moq"),
                    "brands_worked_with": supplier_data.get("brands_worked_with"),
                    "supplier_type": supplier_data.get("supplier_type"),
                    "product_categories": supplier_data.get("product_categories"),
                    "market_experience": supplier_data.get("market_experience"),
                    "founding_year": supplier_data.get("founding_year"),
                }

                # Qualification score (0-100)
                q = 0
                if _fields["legal_name"] and len(str(_fields["legal_name"]).strip()) > 3: q += 15
                if _fields["email"] and "@" in str(_fields["email"]): q += 10
                if _fields["contact_name"] and len(str(_fields["contact_name"]).strip()) > 2: q += 10
                if _fields["wechat_id"] and len(str(_fields["wechat_id"]).strip()) > 2: q += 5
                certs_list = [c.strip() for c in str(_fields.get("certs_and_audits","")).split(";") if c.strip()] if _fields.get("certs_and_audits") else []
                if len(certs_list) >= 5: q += 20
                elif len(certs_list) >= 3: q += 15
                elif len(certs_list) >= 1: q += 10
                if _fields["factory_locations"] and len(str(_fields["factory_locations"]).strip()) > 2: q += 10
                if _fields["moq"] and len(str(_fields["moq"]).strip()) > 0: q += 10
                if _fields["brands_worked_with"] and len(str(_fields["brands_worked_with"]).strip()) > 2: q += 10
                if _fields["supplier_type"]:
                    if "Manufacturer" in str(_fields["supplier_type"]): q += 10
                    elif "Trading" in str(_fields["supplier_type"]): q += 5
                supplier_data["qualification_score"] = min(q, 100)

                # Completeness score (0-100)
                filled = sum(1 for v in _fields.values() if v and str(v).strip())
                supplier_data["data_completeness_score"] = round((filled / len(_fields)) * 100)

                # Save to DB
                sid = save_supplier(supplier_data, brief_id)
                if sid > 0:
                    saved_count += 1
                    existing_names.add(supplier_data["trade_name"].lower().strip())

                feedback = f"Extracted data for '{supplier_data.get('trade_name', 'unknown')}': {json.dumps(supplier_data, indent=2)[:500]}\n\nSaved to DB. Suppliers so far: {saved_count}/{max_suppliers}. Continue searching."
            else:
                feedback = f"Could not extract supplier data from {url}. The page may be empty/JS-only or not a real supplier. Try a different approach.\n\nSuppliers so far: {saved_count}/{max_suppliers}."

            # Update the running count in the next prompt
            feedback += f"\n\nExisting suppliers found (DO NOT duplicate): {', '.join(sorted(existing_names)[:30])}"
            messages.append({"role": "user", "content": feedback})

            time.sleep(1)  # Rate limit scraping

        elif action == "save":
            try:
                supplier_data = json.loads(params)
                if not supplier_data.get("trade_name"):
                    messages.append({"role": "user", "content": "ERROR: trade_name is required for save. Format: {\"trade_name\": \"...\", ...}"})
                    continue

                supplier_data.setdefault("product_categories", category)
                supplier_data["match_score"] = 80

                # Compute scores for save action too
                _f = {
                    "legal_name": supplier_data.get("legal_name"),
                    "email": supplier_data.get("email"),
                    "contact_name": supplier_data.get("contact_name"),
                    "wechat_id": supplier_data.get("wechat_id"),
                    "factory_locations": supplier_data.get("factory_locations"),
                    "certs_and_audits": supplier_data.get("certs_and_audits"),
                    "moq": supplier_data.get("moq"),
                    "brands_worked_with": supplier_data.get("brands_worked_with"),
                    "supplier_type": supplier_data.get("supplier_type"),
                    "product_categories": supplier_data.get("product_categories"),
                    "market_experience": supplier_data.get("market_experience"),
                    "founding_year": supplier_data.get("founding_year"),
                }
                q = 0
                if _f["legal_name"] and len(str(_f["legal_name"]).strip()) > 3: q += 15
                if _f["email"] and "@" in str(_f["email"]): q += 10
                if _f["contact_name"] and len(str(_f["contact_name"]).strip()) > 2: q += 10
                if _f["wechat_id"] and len(str(_f["wechat_id"]).strip()) > 2: q += 5
                _cl = [c.strip() for c in str(_f.get("certs_and_audits","")).split(";") if c.strip()] if _f.get("certs_and_audits") else []
                if len(_cl) >= 5: q += 20
                elif len(_cl) >= 3: q += 15
                elif len(_cl) >= 1: q += 10
                if _f["factory_locations"] and len(str(_f["factory_locations"]).strip()) > 2: q += 10
                if _f["moq"] and len(str(_f["moq"]).strip()) > 0: q += 10
                if _f["brands_worked_with"] and len(str(_f["brands_worked_with"]).strip()) > 2: q += 10
                if _f["supplier_type"]:
                    if "Manufacturer" in str(_f["supplier_type"]): q += 10
                    elif "Trading" in str(_f["supplier_type"]): q += 5
                supplier_data["qualification_score"] = min(q, 100)
                filled = sum(1 for v in _f.values() if v and str(v).strip())
                supplier_data["data_completeness_score"] = round((filled / len(_f)) * 100)

                sid = save_supplier(supplier_data, brief_id)
                if sid > 0:
                    saved_count += 1
                    existing_names.add(supplier_data["trade_name"].lower().strip())

                messages.append({
                    "role": "user",
                    "content": f"Saved supplier '{supplier_data.get('trade_name')}'. Count: {saved_count}/{max_suppliers}. Continue."
                })
            except json.JSONDecodeError:
                messages.append({"role": "user", "content": f"ERROR: Invalid JSON: {params}"})

        else:
            messages.append({"role": "user", "content": f"Unknown action '{action}'. Use: search, scrape, save, or done."})

    # Final status
    if saved_count >= max_suppliers:
        update_brief_status(brief_id, "ENRICHED")
        logger.info(f"Research complete: {saved_count} suppliers found for brief {brief_id}")
    elif saved_count > 0:
        update_brief_status(brief_id, "PARTIALLY_ENRICHED")
        logger.info(f"Research partial: {saved_count} suppliers found for brief {brief_id}")
    else:
        update_brief_status(brief_id, "ENRICHMENT_FAILED")
        logger.warning(f"Research failed: no suppliers found for brief {brief_id}")

    return saved_count


# ─── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research suppliers for a product brief")
    parser.add_argument("--brief-id", type=int, required=True, help="Brief ID to research")
    parser.add_argument("--max-suppliers", type=int, default=MAX_SUPPLIERS, help="Max suppliers to find")
    args = parser.parse_args()

    count = research_brief(args.brief_id, args.max_suppliers)
    print(f"\nResearch complete: {count} suppliers found")
