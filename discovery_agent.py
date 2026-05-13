"""
Atelier Discovery Agent — Finds manufacturers matching a product brief.

Strategy:
1. Parse the brief to extract search keywords (product type, materials, certs, country)
2. Run web searches (DuckDuckGo API, site-specific B2B searches) to find candidate suppliers
3. Score each candidate against the brief requirements
4. Return structured supplier data ready for DB insertion

Uses ddgs (DuckDuckGo Search) for reliable programmatic search.
Falls back to existing DB suppliers if web search yields nothing.
"""
import os, re, json, logging
from typing import Optional
from datetime import datetime

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discovery-agent")

# ─── Brief Parsing ────────────────────────────────────────────
# Product-category → search keyword templates
CATEGORY_KEYWORDS = {
    "towel": ["bath towel manufacturer", "hotel towel supplier", "beach towel OEM"],
    "moisturizer": ["moisturizer manufacturer", "skincare manufacturer OEM", "whipped moisturizer private label"],
    "tallow": ["tallow moisturizer manufacturer", "beef tallow skincare OEM", "whipped tallow private label"],
    "candle": ["candle manufacturer", "soy candle private label", "candle OEM supplier"],
    "skincare": ["skincare manufacturer OEM", "cosmetics private label", "skincare contract manufacturer"],
    "textile": ["textile manufacturer OEM", "home textile supplier", "fabric manufacturer"],
}

def extract_search_queries(brief: dict) -> list[dict]:
    """Turn a brief into targeted search queries for supplier discovery."""
    queries = []
    desc = brief.get("description", "").lower()
    category = (brief.get("category", "") or brief.get("product_name", "")).lower()
    product_name = brief.get("product_name", "")
    country = brief.get("country_of_origin", "")
    certs = brief.get("certifications_required", "")
    ingredients = brief.get("key_ingredients", "")
    formulation = brief.get("formulation_type", "")

    # Determine product keywords from category/description
    product_keywords = []
    for cat_key, keywords in CATEGORY_KEYWORDS.items():
        if cat_key in category or cat_key in desc:
            product_keywords = keywords
            break
    if not product_keywords:
        # Generic: use the product name
        product_keywords = [f"{product_name.lower()} manufacturer"] if product_name else ["manufacturer supplier"]

    def _deduped_query(base: str, suffix: str) -> str:
        """Remove suffix words already present in base (case-insensitive)."""
        base_words = set(base.lower().split())
        suffix_words = suffix.split()
        filtered = [w for w in suffix_words if w.lower() not in base_words]
        return f"{base} {' '.join(filtered)}".strip()

    for pk in product_keywords[:2]:  # Limit to top 2 product keywords
        # Variant 1: General web search
        queries.append({
            "engine": "ddgs",
            "query": _deduped_query(pk, "manufacturer supplier OEM ODM wholesale"),
        })
        # Variant 2: Alibaba-specific search
        queries.append({
            "engine": "ddgs",
            "query": f"site:alibaba.com {pk} OEM",
        })
        # Variant 3: Made-in-China search
        queries.append({
            "engine": "ddgs",
            "query": f"site:made-in-china.com {pk}",
        })
        # Variant 4: Country-specific if specified
        if country:
            queries.append({
                "engine": "ddgs",
                "query": f"{pk} {country} manufacturer factory",
            })
        # Variant 5: Cert-specific
        if certs:
            cert_short = certs.split(",")[0].strip()
            queries.append({
                "engine": "ddgs",
                "query": f"{pk} {cert_short} certified manufacturer",
            })
        # Variant 6: Formulation type
        if formulation and formulation.lower() not in pk.lower():
            queries.append({
                "engine": "ddgs",
                "query": f"{pk} {formulation} contract manufacturer",
            })

    return queries


# ─── DuckDuckGo Search (via ddgs library) ──────────────────────
def search_ddgs(query: str, max_results: int = 10) -> list[dict]:
    """Search DuckDuckGo using the ddgs library (proper API, not HTML scraping)."""
    results = []
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("url", "")),
                    "snippet": r.get("body", r.get("description", "")),
                })
    except ImportError:
        # Fallback to duckduckgo_search if ddgs not available
        try:
            from duckduckgo_search import DDGS as DDGS2
            with DDGS2() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href", r.get("url", "")),
                        "snippet": r.get("body", r.get("description", "")),
                    })
        except Exception as e:
            logger.warning(f"DDGo search failed for '{query}': {e}")
    except Exception as e:
        logger.warning(f"DDGo search failed for '{query}': {e}")
    return results[:max_results]


# ─── Supplier Extraction from Search Results ──────────────────
def extract_suppliers_from_results(results: list[dict], brief: dict) -> list[dict]:
    """Parse search results into structured supplier records."""
    suppliers = []
    seen_names = set()

    for r in results:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        company = r.get("company", "") or title

        # Skip non-manufacturer / consumer-facing / aggregator results
        skip_words = [
            "wikipedia", "youtube", "reddit", "amazon.com", "amazon.", "ebay", "etsy",
            "pinterest", "instagram", "facebook.com/", "twitter.com/", "tiktok",
            "walmart.com", "target.com", "costco.com", "wayfair",
            "buzzfeed", "huffpost", "nytimes", "guardian",
            "thomasnet.com", "accio.com", "alibaba.com", "alibaba.co",
            "made-in-china.com", "globalsources.com", "indiamart.com",
            "tradekey.com", "dhgate.com", "ec21.com", "ecplaza.net",
            "keychain.com", "findmymanufacturer.com", "xiranskincare.com",
            "topbeautyprovider.com", "madeinchina.com", "1688.com",
            "product-insights", "showroom", "product-detail",
            "manufacturers/", "suppliers/", "/find-supplier",
        ]
        if any(w in url.lower() or w in title.lower() for w in skip_words):
            continue

        # Normalize company name: extract from page title or URL
        # Many B2B search results have format: "Product Category - Company Name" or "Company Name — Product"
        name = company.strip()
        
        # Try to extract company name from separator patterns
        # Format 1: "Description — Company" or "Description - Company" → take right side
        for sep in [" — ", " — ", " – ", " | ", " - "]:
            parts = name.split(sep)
            if len(parts) >= 2:
                # Heuristic: the shorter part is likely the company name
                candidates = [(p.strip(), len(p.strip())) for p in parts if len(p.strip()) > 2]
                if candidates:
                    # Pick the shortest meaningful part (likely the company name, not the product description)
                    candidates.sort(key=lambda x: x[1])
                    name = candidates[0][0]
                    break
        
        # Strip trailing legal entities
        name = re.sub(r"\s*(Co\.?\s*,?\s*Ltd\.?|Inc\.?|LLC|Corp\.?|Corporation|Group|Holdings?|Pte\.?\s*Ltd\.?)\s*$", "", name, flags=re.I).strip()
        
        # If name is still too long or looks like a product listing, try extracting from URL
        if len(name) > 50 or re.search(r'(private label|top \d+|best |cheap |wholesale |custom |oem |odm |manufacturer|supplier|factory)', name, re.I):
            # Try to extract brand from URL
            url_name = _extract_brand_from_url(url)
            if url_name:
                name = url_name
        
        # Skip obvious non-company names (nav elements, generic words)
        generic_names = {"home", "about", "contact", "products", "services", "company overview",
                        "company profile", "catalog", "login", "register", "search", "amazon"}
        if name.lower().strip() in generic_names:
            continue
        
        if not name or len(name) < 3:
            continue

        # Deduplicate
        name_key = name.lower().replace(" ", "")
        if name_key in seen_names:
            continue
        seen_names.add(name_key)

        # Clean up URL
        clean_url = url if url.startswith("http") else f"https://{url}" if url else ""

        # Build supplier record
        supplier = {
            "trade_name": name,
            "website": clean_url,
            "outreach_state": "DISCOVERED",
            "supplier_type": "Manufacturer",
            "product_categories": brief.get("category", ""),
            "date_created": datetime.utcnow().strftime("%Y-%m-%d"),
            "certs_and_audits": _extract_certs_from_text(snippet + " " + title),
            "factory_locations": _extract_country_from_text(snippet + " " + title),
            "match_score": r.get("match_score_override") or _score_match(snippet + " " + title, brief),
        }
        suppliers.append(supplier)

    return suppliers


def _extract_brand_from_url(url: str) -> str:
    """Extract a brand/company name from a URL domain."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        # Remove www. and common prefixes
        domain = re.sub(r'^www\.', '', domain)
        # Remove common B2B platform domains → return empty (not a real company)
        if any(p in domain for p in ['alibaba.com', 'alibaba.co', 'made-in-china.com', 'thomasnet.com',
                                       'globalsources.com', 'indiamart.com', 'tradekey.com',
                                       'dhgate.com', 'ec21.com', 'ecplaza.net',
                                       'accio.com', 'keychain.com', 'findmymanufacturer.com',
                                       '1688.com']):
            return ""
        # Take the subdomain or main domain part
        parts = domain.split('.')
        if len(parts) >= 2:
            # Use subdomain if it's not 'www' or generic
            subdomain = parts[0]
            if subdomain not in ('www', 'en', 'm', 'api', 'mail', 'shop'):
                # Convert subdomain to title case
                name = subdomain.replace('-', ' ').replace('_', ' ').title()
                return name
        return ""
    except Exception:
        return ""


def _extract_certs_from_text(text: str) -> str:
    """Extract certification mentions from text."""
    cert_patterns = [
        (r"OEKO-TEX|Oeko[- ]Tex", "OEKO-TEX Standard 100"),
        (r"ISO\s*9001", "ISO 9001"),
        (r"ISO\s*14001", "ISO 14001"),
        (r"GOTS", "GOTS"),
        (r"GRS", "GRS"),
        (r"BSCI", "BSCI"),
        (r"WRAP", "WRAP"),
        (r"SEDEX", "SEDEX"),
        (r"Fair\s*Trade", "Fair Trade"),
        (r"FDA\s+Registered|FDA\s+Compliant", "FDA Registered"),
        (r"cGMP", "cGMP"),
        (r"GMP", "GMP"),
        (r"ISO\s*22716", "ISO 22716"),
    ]
    found = []
    for pattern, label in cert_patterns:
        if re.search(pattern, text, re.I):
            found.append(label)
    return "; ".join(found)


def _extract_country_from_text(text: str) -> str:
    """Extract country mentions from text."""
    country_map = {
        "China": "China", "Chinese": "China", "Shandong": "China", "Zhejiang": "China",
        "Jiangsu": "China", "Guangdong": "China", "Japan": "Japan", "Japanese": "Japan",
        "Imabari": "Japan", "Vietnam": "Vietnam", "Indonesia": "Indonesia",
        "Thailand": "Thailand", "Malaysia": "Malaysia", "Taiwan": "Taiwan",
        "India": "India", "USA": "USA", "United States": "USA",
        "South Korea": "South Korea", "Turkey": "Turkey", "Pakistan": "Pakistan",
        "UK": "UK", "United Kingdom": "UK", "Germany": "Germany",
        "Italy": "Italy", "France": "France", "Brazil": "Brazil",
        "Mexico": "Mexico", "Canada": "Canada", "Australia": "Australia",
    }
    for pattern, country in country_map.items():
        if pattern.lower() in text.lower():
            return country
    return ""


def _score_match(text: str, brief: dict) -> float:
    """Score how well a search result matches the brief (0-100)."""
    score = 30.0  # Base score for showing up
    text_lower = text.lower()
    desc_lower = brief.get("description", "").lower()
    product_lower = brief.get("product_name", "").lower()
    certs_required = brief.get("certifications_required", "").lower()
    country = brief.get("country_of_origin", "").lower()

    # Product name match
    product_words = [w for w in product_lower.split() if len(w) > 3]
    for w in product_words:
        if w in text_lower:
            score += 8

    # OEM/ODM/manufacturer signals
    if any(w in text_lower for w in ["oem", "odm", "manufacturer", "factory", "supplier", "wholesale", "private label", "contract manufacturing"]):
        score += 10

    # Certification match
    if certs_required:
        for cert in certs_required.split(","):
            cert = cert.strip().lower()
            if cert and cert in text_lower:
                score += 5

    # Country match
    if country:
        for c in country.split(","):
            c = c.strip().lower()
            if c and c in text_lower:
                score += 5

    # Brief description keyword overlap
    desc_words = [w for w in desc_lower.split() if len(w) > 4]
    for w in desc_words:
        if w in text_lower:
            score += 2

    return min(score, 100.0)


# ─── Main Discovery Function ─────────────────────────────────
def discover_suppliers(brief: dict) -> list[dict]:
    """
    Main entry point: take a brief dict, search for matching suppliers.
    Returns a list of supplier dicts ready for DB insertion.

    Strategy: Web search via DDGS API, fallback to existing DB match.
    """
    logger.info(f"Discovering suppliers for: {brief.get('product_name', 'unknown')}")

    queries = extract_search_queries(brief)
    all_results = []

    for q in queries:
        engine = q.get("engine", "ddgs")
        query_text = q.get("query", "")

        if engine == "ddgs":
            results = search_ddgs(query_text)
        else:
            results = search_ddgs(query_text)  # Default to DDGS for all engines

        all_results.extend(results)
        logger.info(f"  {engine}: '{query_text}' → {len(results)} results")

    # If web search returned nothing, fall back to existing DB match
    db_fallback_suppliers = []
    if not all_results:
        logger.info("Web search returned no results, falling back to existing DB match")
        db_fallback_suppliers = match_existing_suppliers(brief)

    # Deduplicate and extract supplier records from web search results
    suppliers = extract_suppliers_from_results(all_results, brief) if all_results else []

    # Merge DB fallback suppliers (they're already in supplier format)
    suppliers.extend(db_fallback_suppliers)

    # Sort by match score
    suppliers.sort(key=lambda s: s.get("match_score", 0), reverse=True)

    # Cap at top 20
    suppliers = suppliers[:20]

    logger.info(f"Discovered {len(suppliers)} candidate suppliers")
    return suppliers


def match_existing_suppliers(brief: dict) -> list[dict]:
    """Match a brief against existing suppliers in the DB as fallback.
    Returns supplier dicts with 'existing_supplier_id' for direct linking."""
    results = []
    try:
        import sqlite3
        db_path = os.environ.get("DB_PATH", "/data/luxury_towel_suppliers/suppliers.db")
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        category = brief.get("category", "").lower()
        product = brief.get("product_name", "").lower()
        desc = brief.get("description", "").lower()
        certs = brief.get("certifications_required", "").lower()
        country = brief.get("country_of_origin", "").lower()

        rows = db.execute("SELECT * FROM suppliers").fetchall()
        for row in rows:
            s = dict(row)
            text = f"{s.get('product_sub_categories','')} {s.get('certs_and_audits','')} {s.get('factory_locations','')} {s.get('trade_name','')} {s.get('brands_worked_with','')}".lower()
            score = 20.0

            if category and any(w in text for w in category.split()):
                score += 15
            product_words = [w for w in product.split() if len(w) > 3]
            for w in product_words:
                if w in text:
                    score += 8
            if certs:
                for c in certs.split(","):
                    c = c.strip().lower()
                    if c and c in text:
                        score += 5
            if country:
                for c in country.split(","):
                    c = c.strip().lower()
                    if c and c in text:
                        score += 5
            desc_words = [w for w in desc.split() if len(w) > 4]
            for w in desc_words:
                if w in text:
                    score += 2

            if score >= 25:
                supplier = {
                    "trade_name": s.get("trade_name", ""),
                    "website": s.get("website", ""),
                    "outreach_state": "DISCOVERED",
                    "supplier_type": s.get("supplier_type", "Manufacturer"),
                    "product_categories": s.get("product_sub_categories", ""),
                    "date_created": datetime.utcnow().strftime("%Y-%m-%d"),
                    "certs_and_audits": s.get("certs_and_audits", ""),
                    "factory_locations": s.get("factory_locations", ""),
                    "match_score": min(score, 100),
                    "existing_supplier_id": s.get("id"),
                }
                results.append(supplier)

        db.close()
    except Exception as e:
        logger.warning(f"DB fallback match failed: {e}")

    return results


# ─── Enrichment ────────────────────────────────────────────────
def enrich_suppliers(suppliers: list[dict]) -> list[dict]:
    """
    Enrich supplier records with additional data from their websites.
    Sets outreach_state to ENRICHED.
    """
    enriched = []
    for s in suppliers:
        website = s.get("website", "")
        if website and website.startswith("http"):
            try:
                site_data = scrape_supplier_website(website)
                if site_data:
                    # Merge: don't overwrite existing data
                    for k, v in site_data.items():
                        if v and not s.get(k):
                            s[k] = v
            except Exception as e:
                logger.warning(f"Failed to scrape {website}: {e}")

        s["outreach_state"] = "ENRICHED"
        s["data_completeness_score"] = _compute_completeness(s)
        enriched.append(s)

    return enriched


def scrape_supplier_website(url: str) -> dict:
    """Scrape a supplier's website for additional info.
    Only populates fields that exist in the suppliers DB schema."""
    VALID_COLS = {
        "trade_name", "legal_name", "factory_locations", "supplier_type",
        "supplier_subtype", "flags", "product_categories", "product_sub_categories",
        "certs_and_audits", "regulatory_compliance", "brands_worked_with",
        "contact_name", "market_experience", "certification_link", "ip_ownership",
        "moq", "moq_info", "email", "website"
    }
    data = {}
    try:
        with httpx.Client(timeout=10, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return data
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Extract email
            email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
            if email_match:
                data["email"] = email_match.group(0)

            # Extract certifications from page
            certs = _extract_certs_from_text(text)
            if certs:
                data["certs_and_audits"] = certs

            # Extract location
            location = _extract_country_from_text(text)
            if location:
                data["factory_locations"] = location

            # Extract brand mentions
            brand_patterns = re.findall(r'(?:works? with|suppl(?:y|ies|ied) (?:to|for)|clients? (?:include|:))\s*([A-Z][\w\s&,;.]+)', text)
            if brand_patterns:
                data["brands_worked_with"] = brand_patterns[0].strip()[:200]

            # Extract MOQ
            moq_match = re.search(r'(\d[\d,–\-]+\s*(?:pcs|pieces|units|items))\s*(?:/|per)', text, re.I)
            if moq_match:
                data["moq"] = moq_match.group(1).strip()

            # Only return fields that exist in the DB schema
            data = {k: v for k, v in data.items() if k in VALID_COLS}
    except Exception as e:
        logger.debug(f"Scrape error for {url}: {e}")

    return data


def _compute_completeness(supplier: dict) -> int:
    """Compute data completeness score (0-100)."""
    key_fields = [
        "trade_name", "factory_locations", "supplier_type", "certs_and_audits",
        "brands_worked_with", "moq", "email", "website", "wechat_id"
    ]
    filled = sum(1 for f in key_fields if supplier.get(f) and str(supplier[f]).strip() not in ("", "null", "None"))
    return int((filled / len(key_fields)) * 100)


# ─── CLI test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    test_brief = {
        "product_name": "Whipped Tallow Moisturizer",
        "description": "Looking to replicate a tallow moisturizer with copper peptides and manuka honey. White label preferred. Made in USA non-negotiable.",
        "category": "Skincare",
        "country_of_origin": "USA",
        "certifications_required": "cGMP, FDA Registered",
        "formulation_type": "White Label",
        "key_ingredients": "Tallow, Copper Peptides, Manuka Honey",
    }
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        results = discover_suppliers(test_brief)
        for r in results:
            print(f"  [{r.get('match_score',0):.0f}] {r.get('trade_name','?')} — {r.get('factory_locations','')} — {r.get('website','')}")
