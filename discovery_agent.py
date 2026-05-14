"""
Atelier Discovery Agent — Finds manufacturers matching a product brief.

Strategy (v2 — deep multi-pass):
1. Intelligently parse the brief to extract product type, materials, region, certs
2. Run 6-8 targeted search queries per brief (general, B2B directories, regional,
   certification, category-specific, industry terms)
3. VALIDATE each result — reject consumer sites, news/media, directory listings;
    require manufacturer/supplier signals; optionally HTTP-GET the URL for contact
    /about/products sections
4. Score with weighted signals: base 20, +15 OEM/ODM, +10 cert, +10 country,
   +5 per product keyword, -20 no website, -30 news/blog
5. Deduplicate by domain (not just trade_name)
6. Cap at top 20 sorted by match_score
7. Fall back to existing DB suppliers if web search yields nothing

Uses ddgs (DuckDuckGo Search) for reliable programmatic search.
Uses httpx + bs4 for HTTP-based result validation.
"""
import os
import re
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("discovery-agent")

# ─── Category → search keyword templates ──────────────────────────
CATEGORY_KEYWORDS = {
    "towel": ["bath towel manufacturer", "hotel towel supplier", "beach towel OEM",
              "textile towel factory private label"],
    "moisturizer": ["moisturizer manufacturer OEM", "skincare contract manufacturer",
                    "whipped moisturizer private label", "lotion manufacturer wholesale"],
    "tallow": ["tallow moisturizer manufacturer", "beef tallow skincare OEM",
               "whipped tallow private label", "tallow balm manufacturer"],
    "candle": ["candle manufacturer OEM", "soy candle private label",
              "candle factory wholesale", "wax candle contract manufacturer"],
    "skincare": ["skincare manufacturer OEM", "cosmetics private label",
                 "skincare contract manufacturer", "beauty products OEM wholesale"],
    "textile": ["textile manufacturer OEM", "home textile supplier",
                "fabric manufacturer wholesale", "textile factory private label"],
    "eyewear": ["eyewear manufacturer OEM", "sunglasses factory wholesale",
                "optical frames manufacturer ODM", "eyewear private label supplier"],
    "jewelry": ["jewelry manufacturer OEM", "jewelry factory wholesale",
                "fine jewelry private label", "jewelry contract manufacturer"],
    "watch": ["watch manufacturer OEM", "watch factory wholesale",
              "wristwatch private label supplier", "watch contract manufacturer"],
    "apparel": ["apparel manufacturer OEM", "clothing factory wholesale",
                "garment private label supplier", "fashion contract manufacturer"],
    "bags": ["bag manufacturer OEM", "handbag factory wholesale",
             "luggage private label supplier", "bag contract manufacturer"],
    "footwear": ["footwear manufacturer OEM", "shoe factory wholesale",
                 "shoes private label supplier", "footwear contract manufacturer"],
}

# ─── Consumer / news / directory domains to reject ────────────────
CONSUMER_DOMAINS = {
    "amazon.com", "amazon.co.uk", "amazon.co.jp", "amazon.de", "amazon.ca",
    "walmart.com", "target.com", "costco.com", "ebay.com", "etsy.com",
    "wayfair.com", "homedepot.com", "lowes.com", "bestbuy.com",
    "zappos.com", "overstock.com", "wish.com", "temu.com", "shein.com",
    "aliexpress.com", "flipkart.com",
}

NEWS_MEDIA_DOMAINS = {
    "wikipedia.org", "youtube.com", "reddit.com", "pinterest.com",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "tiktok.com",
    "linkedin.com", "quora.com", "medium.com", "buzzfeed.com", "huffpost.com",
    "nytimes.com", "theguardian.com", "bbc.com", "cnn.com", "forbes.com",
    "wsj.com", "bloomberg.com", "reuters.com", "techcrunch.com",
    "yelp.com", "tripadvisor.com", "glassdoor.com",
}

DIRECTORY_DOMAINS = {
    "alibaba.com", "alibaba.co", "made-in-china.com", "madeinchina.com",
    "globalsources.com", "indiamart.com", "tradekey.com", "dhgate.com",
    "ec21.com", "ecplaza.net", "thomasnet.com", "accio.com",
    "1688.com", "keychain.com", "findmymanufacturer.com",
    "xiranskincare.com", "topbeautyprovider.com",
    "indiamart.com", "exportersindia.com", "tradeindia.com",
    "dir.indiamart.com", "krootez.com", "kompass.com",
}

# URL path fragments that indicate directory listing pages, not actual companies
DIRECTORY_PATH_FRAGMENTS = [
    "/manufacturer/", "/suppliers/", "/factory/", "/producer/",
    "/wholesaler/", "/find-supplier", "/search/", "/category/",
    "product-insights", "showroom", "product-detail",
    "/companies/", "/catalog/", "/listing/",
]

# Signals that indicate a B2B / manufacturer result
MANUFACTURER_SIGNALS = [
    "oem", "odm", "manufacturer", "factory", "supplier", "wholesale",
    "private label", "contract manufacturing", "contract manufacturer",
    "white label", "custom manufacturer", "production", "fabrication",
    "foundry", "mill", "plant",
]


# ─── Brief Parsing ───────────────────────────────────────────────
class BriefInfo:
    """Parsed brief with extracted search-relevant fields."""

    def __init__(self, brief: dict):
        self.raw = brief
        self.product_name = (brief.get("product_name") or "").strip()
        self.category = (brief.get("category") or "").strip()
        self.description = (brief.get("description") or "").strip()
        self.country = (brief.get("country_of_origin") or "").strip()
        self.certs = (brief.get("certifications_required") or "").strip()
        self.formulation = (brief.get("formulation_type") or "").strip()
        self.ingredients = (brief.get("key_ingredients") or "").strip()
        self.materials = (brief.get("materials") or "").strip()

        # Lowercase versions for matching
        self.product_lower = self.product_name.lower()
        self.category_lower = self.category.lower()
        self.desc_lower = self.description.lower()

        # Derived fields
        self.product_type = self._extract_product_type()
        self.country_list = [c.strip() for c in self.country.split(",") if c.strip()] if self.country else []
        self.cert_list = [c.strip() for c in self.certs.split(",") if c.strip()] if self.certs else []
        self.material_list = [m.strip() for m in (self.materials or self.ingredients).split(",") if m.strip()]

    def _extract_product_type(self) -> str:
        """Extract the core product noun from product_name or category."""
        # Check category keywords first
        for cat_key in CATEGORY_KEYWORDS:
            if cat_key in self.category_lower or cat_key in self.desc_lower:
                return cat_key
        # Fallback: take the last meaningful word from product_name
        words = self.product_lower.split()
        stopwords = {"whipped", "custom", "premium", "luxury", "organic", "natural",
                     "best", "top", "cheap", "high", "quality", "white", "black"}
        for w in reversed(words):
            if len(w) > 3 and w not in stopwords:
                return w
        return self.product_lower if self.product_lower else "product"


def _build_search_queries(brief_info: BriefInfo) -> list[dict]:
    """Build 6-8 targeted search queries from parsed brief."""
    queries = []
    product = brief_info.product_type
    product_name = brief_info.product_name

    # ── Query 1: General manufacturer search ──
    queries.append({
        "query": f'"{product}" manufacturer OEM ODM',
        "label": "general",
    })

    # ── Query 2: B2B directory search ──
    queries.append({
        "query": f'{product} manufacturer site:alibaba.com OR site:made-in-china.com OR site:globalsources.com',
        "label": "b2b_directory",
    })

    # ── Query 3: Regional search (if country specified) ──
    if brief_info.country:
        primary_country = brief_info.country_list[0]
        queries.append({
            "query": f'{product} factory {primary_country} manufacturer',
            "label": "regional",
        })

    # ── Query 4: Certification search (if certs specified) ──
    if brief_info.certs:
        primary_cert = brief_info.cert_list[0]
        queries.append({
            "query": f'{product} {primary_cert} certified manufacturer',
            "label": "certification",
        })

    # ── Query 5: Category-specific keywords ──
    cat_keywords = CATEGORY_KEYWORDS.get(product, [])
    if cat_keywords:
        # Pick a keyword that differs from the general query
        for kw in cat_keywords:
            if kw.lower() not in queries[0]["query"].lower():
                queries.append({
                    "query": kw,
                    "label": "category_specific",
                })
                break
        else:
            # All keywords overlap — add the second one anyway with different framing
            if len(cat_keywords) > 1:
                queries.append({
                    "query": f'{cat_keywords[1]} factory supplier',
                    "label": "category_specific",
                })

    # ── Query 6: Industry / wholesale terms ──
    queries.append({
        "query": f'{product} supplier wholesale private label',
        "label": "industry",
    })

    # ── Query 7: Formulation / material-specific (if applicable) ──
    if brief_info.formulation and brief_info.formulation.lower() not in product:
        queries.append({
            "query": f'{product} {brief_info.formulation} contract manufacturer',
            "label": "formulation",
        })
    elif brief_info.material_list:
        primary_material = brief_info.material_list[0]
        if primary_material.lower() not in product:
            queries.append({
                "query": f'{primary_material} {product} manufacturer OEM',
                "label": "material",
            })

    # ── Query 8: Broad supplier search with product name ──
    if product_name and product_name.lower() != product:
        queries.append({
            "query": f'"{product_name}" manufacturer supplier',
            "label": "product_name_exact",
        })

    return queries


# ─── DuckDuckGo Search ───────────────────────────────────────────
def _search_ddgs(query: str, max_results: int = 10) -> list[dict]:
    """Search DuckDuckGo using the ddgs library."""
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
            logger.warning(f"DDG search failed for '{query}': {e}")
    except Exception as e:
        logger.warning(f"DDG search failed for '{query}': {e}")
    return results[:max_results]


# ─── Result Validation ────────────────────────────────────────────
def _extract_domain(url: str) -> str:
    """Extract the registered domain (e.g. 'example.com') from a URL."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or parsed.path or "").lower()
        host = re.sub(r"^www\.", "", host)
        # Split and take last two parts for domain (handles subdomains)
        parts = host.split(":")[0].split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


def _is_valid_result(result: dict) -> bool:
    """Validate a search result: reject consumer/news/directory sites and
    results lacking manufacturer signals."""
    url = (result.get("url") or "").lower()
    title = (result.get("title") or "").lower()
    snippet = (result.get("snippet") or "").lower()
    domain = _extract_domain(url)

    # ── Reject consumer e-commerce sites ──
    if domain in CONSUMER_DOMAINS:
        return False

    # ── Reject news / media / social sites ──
    if domain in NEWS_MEDIA_DOMAINS:
        return False

    # ── Reject B2B directory listing sites ──
    if domain in DIRECTORY_DOMAINS:
        return False

    # ── Reject directory listing pages by URL path ──
    for frag in DIRECTORY_PATH_FRAGMENTS:
        if frag in url:
            return False

    # ── Must contain at least one manufacturer/supplier signal ──
    combined_text = f"{title} {snippet}"
    if not any(signal in combined_text for signal in MANUFACTURER_SIGNALS):
        return False

    return True


def _validate_url_http(url: str) -> bool:
    """HTTP GET the URL. Returns True if it 200s and appears to have
    contact/about/products sections (i.e., it's a real company site)."""
    try:
        with httpx.Client(timeout=8, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return False
            text = resp.text.lower()
            # Look for at least one indicator of a real company website
            company_indicators = ["contact", "about", "products", "our company",
                                  "company profile", "factory", "manufactur",
                                  "email", "whatsapp", "catalogue", "catalog"]
            return any(ind in text for ind in company_indicators)
    except Exception:
        # Network errors don't necessarily mean it's invalid — just inconclusive
        return False


# ─── Name Extraction ──────────────────────────────────────────────
def _extract_trade_name(title: str, url: str) -> str:
    """Extract a trade name from the search result title and URL.
    Uses multiple heuristics with fallbacks."""
    name = title.strip()

    # Strategy 1: Split on common separators, take shortest meaningful part
    # Many B2B results: "Product Category - Company Name" or "Company Name — Product"
    for sep in [" — ", " – ", " | ", " - "]:
        parts = name.split(sep)
        if len(parts) >= 2:
            candidates = [(p.strip(), len(p.strip())) for p in parts if len(p.strip()) > 2]
            if candidates:
                # Pick the shortest meaningful part (likely the company name)
                candidates.sort(key=lambda x: x[1])
                name = candidates[0][0]
                break

    # Strategy 2: Strip trailing legal entities
    name = re.sub(
        r"\s*(Co\.?\s*,?\s*Ltd\.?|Inc\.?|LLC|Corp\.?|Corporation|Group|Holdings?|"
        r"Pte\.?\s*Ltd\.?|S\.?A\.?|GmbH|AG|BV|NV|S\.?r\.?L\.?)\s*$",
        "", name, flags=re.I
    ).strip()

    # Strategy 3: If name looks like a product listing, extract from URL instead
    product_listing_pattern = (
        r'(private label|top \d+|best |cheap |wholesale |custom |oem |odm |'
        r'manufacturer|supplier|factory|quality|premium|professional)'
    )
    if len(name) > 50 or re.search(product_listing_pattern, name, re.I):
        url_name = _extract_brand_from_url(url)
        if url_name:
            name = url_name

    # Strategy 4: Strip leading/trailing quotes and whitespace
    name = name.strip('"\'""\'')

    # Skip generic non-company names
    generic_names = {
        "home", "about", "contact", "products", "services", "company overview",
        "company profile", "catalog", "login", "register", "search", "amazon",
        "untitled", "page", "default", "index", "welcome",
    }
    if name.lower().strip() in generic_names:
        return ""

    if not name or len(name) < 3:
        return ""

    return name


def _extract_brand_from_url(url: str) -> str:
    """Extract a brand/company name from a URL domain."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        domain = parsed.netloc or parsed.path
        domain = re.sub(r"^www\.", "", domain)

        # Remove B2B platform domains → not a real company URL
        if any(p in domain for p in DIRECTORY_DOMAINS | CONSUMER_DOMAINS | NEWS_MEDIA_DOMAINS):
            return ""

        parts = domain.split(".")
        if len(parts) >= 2:
            subdomain = parts[0]
            # Skip generic subdomains
            if subdomain not in ("www", "en", "m", "api", "mail", "shop",
                                  "blog", "store", "cdn", "static", "img"):
                name = subdomain.replace("-", " ").replace("_", " ").title()
                return name
        return ""
    except Exception:
        return ""


# ─── Scoring ─────────────────────────────────────────────────────
def _score_supplier(title: str, snippet: str, url: str,
                    brief_info: BriefInfo, http_valid: Optional[bool] = None) -> float:
    """Score a supplier result against the brief (0-100 scale).

    Scoring rubric:
      base 20
      +15 for OEM/ODM signals
      +10 for cert match
      +10 for country match
      +5 per product keyword match (capped at +20)
      -20 for no website
      -30 for news/blog site
      +5 if HTTP validation confirms real company site
    """
    score = 20.0
    text_lower = f"{title} {snippet}".lower()
    url_lower = url.lower()

    # +15 for OEM/ODM/manufacturer signals in title or snippet
    if any(sig in text_lower for sig in ["oem", "odm"]):
        score += 15
    elif any(sig in text_lower for sig in ["manufacturer", "factory", "contract manufactur"]):
        score += 10  # Slightly less than OEM/ODM but still strong

    # +10 for cert match
    if brief_info.certs:
        cert_match_count = 0
        for cert in brief_info.cert_list:
            cert_lower = cert.lower().strip()
            if cert_lower and cert_lower in text_lower:
                cert_match_count += 1
        if cert_match_count > 0:
            score += min(10, cert_match_count * 5)

    # +10 for country match
    if brief_info.country:
        for country in brief_info.country_list:
            country_lower = country.lower().strip()
            if country_lower and country_lower in text_lower:
                score += 10
                break  # Only award once

    # +5 per product keyword match (capped at +20)
    product_keywords = set()
    for w in brief_info.product_lower.split():
        if len(w) > 3:
            product_keywords.add(w.lower())
    for w in brief_info.category_lower.split():
        if len(w) > 3:
            product_keywords.add(w.lower())
    # Add product type
    product_keywords.add(brief_info.product_type.lower())

    keyword_match_count = sum(1 for kw in product_keywords if kw in text_lower)
    score += min(20, keyword_match_count * 5)

    # -20 for no website
    if not url or not url.startswith("http"):
        score -= 20

    # -30 for news/blog site (detected by domain)
    domain = _extract_domain(url)
    if domain in NEWS_MEDIA_DOMAINS:
        score -= 30

    # +5 bonus if HTTP validation confirmed real company site
    if http_valid is True:
        score += 5
    elif http_valid is False:
        score -= 5

    return max(0.0, min(100.0, score))


# ─── Extraction Helpers ──────────────────────────────────────────
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
        (r"CE\s+Certified|CE\s+Mark", "CE Certified"),
        (r"SGS", "SGS"),
        (r"Intertek", "Intertek"),
    ]
    found = []
    for pattern, label in cert_patterns:
        if re.search(pattern, text, re.I):
            found.append(label)
    return "; ".join(found)


def _extract_country_from_text(text: str) -> str:
    """Extract country mentions from text."""
    country_map = {
        "China": "China", "Chinese": "China", "Shandong": "China",
        "Zhejiang": "China", "Jiangsu": "China", "Guangdong": "China",
        "Guangzhou": "China", "Shenzhen": "China", "Dongguan": "China",
        "Japan": "Japan", "Japanese": "Japan", "Imabari": "Japan",
        "Vietnam": "Vietnam", "Vietnamese": "Vietnam",
        "Indonesia": "Indonesia", "Thailand": "Thailand",
        "Malaysia": "Malaysia", "Taiwan": "Taiwan",
        "India": "India", "USA": "USA", "United States": "USA",
        "America": "USA", "South Korea": "South Korea",
        "Turkey": "Turkey", "Pakistan": "Pakistan",
        "UK": "UK", "United Kingdom": "UK", "England": "UK",
        "Germany": "Germany", "Italy": "Italy", "France": "France",
        "Brazil": "Brazil", "Mexico": "Mexico", "Canada": "Canada",
        "Australia": "Australia", "Portugal": "Portugal",
        "Spain": "Spain", "Netherlands": "Netherlands",
        "Bangladesh": "Bangladesh", "Cambodia": "Cambodia",
        "Sri Lanka": "Sri Lanka", "Myanmar": "Myanmar",
    }
    found = set()
    text_lower = text.lower()
    for pattern, country in country_map.items():
        if pattern.lower() in text_lower:
            found.add(country)
    return "; ".join(sorted(found)) if found else ""


# ─── Supplier Extraction from Search Results ─────────────────────
def _extract_suppliers_from_results(results: list[dict], brief_info: BriefInfo) -> list[dict]:
    """Parse validated search results into structured supplier records.
    Deduplicates by domain."""
    suppliers = []
    seen_domains = {}  # domain → index in suppliers list (for dedup by domain)
    seen_names = set()  # normalized name → True

    for r in results:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        http_valid = r.get("http_valid")  # May be None if not checked

        # Clean URL
        clean_url = url if url.startswith("http") else f"https://{url}" if url else ""

        # Domain-level dedup
        domain = _extract_domain(clean_url)
        if domain and domain in seen_domains:
            # Keep the one with the higher score
            existing_idx = seen_domains[domain]
            existing_score = suppliers[existing_idx].get("match_score", 0)
            new_score = _score_supplier(title, snippet, clean_url, brief_info, http_valid)
            if new_score > existing_score:
                # Replace with better result
                suppliers[existing_idx] = _build_supplier_record(
                    title, snippet, clean_url, domain, brief_info, http_valid
                )
            continue

        # Extract trade name
        name = _extract_trade_name(title, clean_url)
        if not name:
            continue

        # Name-level dedup
        name_key = name.lower().replace(" ", "").replace("-", "").replace(".", "")
        if name_key in seen_names:
            continue
        seen_names.add(name_key)

        # Build supplier record
        supplier = _build_supplier_record(title, snippet, clean_url, domain, brief_info, http_valid)
        suppliers.append(supplier)

        # Track domain
        if domain:
            seen_domains[domain] = len(suppliers) - 1

    return suppliers


def _build_supplier_record(title: str, snippet: str, url: str, domain: str,
                           brief_info: BriefInfo, http_valid: Optional[bool]) -> dict:
    """Build a supplier dict ready for DB insertion."""
    name = _extract_trade_name(title, url)
    combined_text = f"{title} {snippet}"

    return {
        "trade_name": name,
        "website": url,
        "outreach_state": "DISCOVERED",
        "supplier_type": "Manufacturer",
        "product_categories": brief_info.category or brief_info.product_type,
        "date_created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "certs_and_audits": _extract_certs_from_text(combined_text),
        "factory_locations": _extract_country_from_text(combined_text),
        "match_score": _score_supplier(title, snippet, url, brief_info, http_valid),
        "discovered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ─── DB Fallback ─────────────────────────────────────────────────
def match_existing_suppliers(brief: dict) -> list[dict]:
    """Match a brief against existing suppliers in the DB as fallback.
    Returns supplier dicts with 'existing_supplier_id' for direct linking."""
    results = []
    try:
        import sqlite3
        db_path = os.environ.get(
            "DB_PATH",
            os.path.join(os.path.dirname(__file__), "suppliers.db"),
        )
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
            text = (
                f"{s.get('product_sub_categories', '')} "
                f"{s.get('certs_and_audits', '')} "
                f"{s.get('factory_locations', '')} "
                f"{s.get('trade_name', '')} "
                f"{s.get('brands_worked_with', '')}"
            ).lower()

            # Score using same rubric as web results
            score = 20.0

            # Category match
            if category and any(w in text for w in category.split()):
                score += 10

            # Product keyword match (capped at +20)
            product_words = [w for w in product.split() if len(w) > 3]
            kw_matches = sum(1 for w in product_words if w in text)
            score += min(20, kw_matches * 5)

            # OEM/ODM signals
            if any(sig in text for sig in ["oem", "odm"]):
                score += 15
            elif any(sig in text for sig in ["manufacturer", "factory"]):
                score += 10

            # Cert match (capped at +10)
            if certs:
                cert_matches = sum(
                    1 for c in certs.split(",") if c.strip().lower() and c.strip().lower() in text
                )
                score += min(10, cert_matches * 5)

            # Country match
            if country:
                for c in country.split(","):
                    c = c.strip().lower()
                    if c and c in text:
                        score += 10
                        break

            # Description keyword overlap (minor bonus)
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
                    "date_created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "certs_and_audits": s.get("certs_and_audits", ""),
                    "factory_locations": s.get("factory_locations", ""),
                    "match_score": min(score, 100.0),
                    "discovered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "existing_supplier_id": s.get("id"),
                }
                results.append(supplier)

        db.close()
    except Exception as e:
        logger.warning(f"DB fallback match failed: {e}")

    return results


# ─── Main Discovery Function ─────────────────────────────────────
def discover_suppliers(brief: dict) -> list[dict]:
    """
    Main entry point: take a brief dict, search for matching suppliers.
    Returns a list of supplier dicts ready for DB insertion.

    Strategy:
      1. Parse brief intelligently (BriefInfo)
      2. Build 6-8 targeted search queries
      3. Execute searches via DDGS
      4. Validate each result (reject consumer/news/directory, require mfg signals)
      5. Optionally HTTP-validate top candidates
      6. Extract supplier records, dedup by domain
      7. Score and sort
      8. Cap at top 20
      9. Fall back to existing DB if web search yields nothing
    """
    brief_info = BriefInfo(brief)
    logger.info(f"Discovering suppliers for: {brief_info.product_name or 'unknown'} "
                f"(type={brief_info.product_type}, country={brief_info.country}, certs={brief_info.certs})")

    # ── Step 1: Build queries ──
    queries = _build_search_queries(brief_info)
    logger.info(f"Built {len(queries)} search queries")

    # ── Step 2: Execute searches ──
    all_results = []
    seen_urls = set()  # Deduplicate raw results by URL

    for q in queries:
        query_text = q["query"]
        label = q.get("label", "unknown")
        try:
            results = _search_ddgs(query_text, max_results=10)
        except Exception as e:
            logger.warning(f"Search failed for '{query_text}': {e}")
            results = []

        # Dedup by URL within results
        new_count = 0
        for r in results:
            url_key = r.get("url", "").lower().rstrip("/")
            if url_key and url_key not in seen_urls:
                seen_urls.add(url_key)
                r["_query_label"] = label
                all_results.append(r)
                new_count += 1

        logger.info(f"  [{label}] '{query_text}' → {len(results)} raw, {new_count} new unique")

    logger.info(f"Total unique raw results: {len(all_results)}")

    # ── Step 3: Validate results ──
    valid_results = []
    for r in all_results:
        if _is_valid_result(r):
            valid_results.append(r)
        else:
            logger.debug(f"Rejected: {r.get('title', '')[:60]} — {r.get('url', '')[:60]}")

    logger.info(f"Valid results after filtering: {len(valid_results)} (rejected {len(all_results) - len(valid_results)})")

    # ── Step 4: HTTP-validate top candidates (async-friendly, cap at 30 to avoid hammering) ──
    http_check_count = 0
    max_http_checks = 30
    for r in valid_results:
        url = r.get("url", "")
        if url and url.startswith("http") and http_check_count < max_http_checks:
            valid = _validate_url_http(url)
            r["http_valid"] = valid
            http_check_count += 1
            if not valid:
                logger.debug(f"HTTP check failed: {url[:80]}")
        else:
            r["http_valid"] = None

    logger.info(f"HTTP-validated {http_check_count} URLs")

    # ── Step 5: Extract supplier records ──
    suppliers = _extract_suppliers_from_results(valid_results, brief_info)

    # ── Step 6: Fall back to DB if web search yielded nothing ──
    if not suppliers:
        logger.info("Web search yielded no valid suppliers, falling back to existing DB match")
        db_suppliers = match_existing_suppliers(brief)
        suppliers.extend(db_suppliers)

    # ── Step 7: Sort by match_score and cap at 20 ──
    suppliers.sort(key=lambda s: s.get("match_score", 0), reverse=True)
    suppliers = suppliers[:20]

    logger.info(f"Discovered {len(suppliers)} candidate suppliers (top score: "
                f"{suppliers[0]['match_score']:.0f}" if suppliers else "Discovered 0 candidate suppliers")

    return suppliers


# ─── CLI test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    test_briefs = [
        {
            "product_name": "Whipped Tallow Moisturizer",
            "description": "Looking to replicate a tallow moisturizer with copper peptides and manuka honey. White label preferred. Made in USA non-negotiable.",
            "category": "Skincare",
            "country_of_origin": "USA",
            "certifications_required": "cGMP, FDA Registered",
            "formulation_type": "White Label",
            "key_ingredients": "Tallow, Copper Peptides, Manuka Honey",
        },
        {
            "product_name": "Luxury Bath Towel",
            "description": "Premium cotton bath towels for hotel and spa use. OEKO-TEX certified. Made in Turkey or Portugal.",
            "category": "Towel",
            "country_of_origin": "Turkey, Portugal",
            "certifications_required": "OEKO-TEX, GOTS",
        },
        {
            "product_name": "Designer Sunglasses",
            "description": "Acetate frame sunglasses manufacturer with polarized lenses. OEM/ODM capabilities required. China preferred.",
            "category": "Eyewear",
            "country_of_origin": "China",
            "certifications_required": "CE, FDA",
        },
    ]

    brief_idx = 0
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        if len(sys.argv) > 2:
            brief_idx = int(sys.argv[2])
        brief = test_briefs[brief_idx]
        print(f"\n=== Testing brief: {brief['product_name']} ===\n")
        results = discover_suppliers(brief)
        for r in results:
            print(f"  [{r.get('match_score', 0):.0f}] {r.get('trade_name', '?')} — "
                  f"{r.get('factory_locations', '')} — {r.get('website', '')}")
