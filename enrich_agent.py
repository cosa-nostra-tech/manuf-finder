"""
Deep enrichment agent for luxury towel supplier records.

Public API:
    enrich_suppliers(suppliers: list[dict]) -> list[dict]

Performs multi-page website scraping, Alibaba/Made-in-China cross-referencing,
and thorough structured data extraction to fill supplier records.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

logger = logging.getLogger("enrich-agent")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REQUEST_TIMEOUT = 10  # seconds
_MAX_PAGES_PER_SUPPLIER = 10
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CRAWL_PATHS = [
    ("homepage", "/"),
    ("about", "/about"),
    ("about_us", "/about-us"),
    ("company", "/company"),
    ("company_profile", "/company-profile"),
    ("contact", "/contact"),
    ("contact_us", "/contact-us"),
    ("products", "/products"),
    ("product", "/product"),
    ("catalog", "/catalog"),
    ("certificates", "/certificates"),
    ("certifications", "/certifications"),
    ("quality", "/quality"),
]

_CERT_KEYWORDS = [
    "OEKO-TEX",
    "Oeko-Tex",
    "oeko-tex",
    "ISO 9001",
    "ISO 14001",
    "ISO 45001",
    "ISO 13485",
    "GOTS",
    "GRS",
    "BSCI",
    "WRAP",
    "FDA",
    "GMP",
    "cGMP",
    "SEDEX",
    "SA8000",
    "HIGG",
    "REACH",
    "AZO",
    "Form A",
    "Form B",
    "C-TPAT",
    "DISNEY",
    "Walmart",
    "Costco",
    "Target",
    "BSCI",
    "FSC",
    "CE",
    "ANSI",
    "AS/NZS",
    "EN 166",
    "EN 167",
    "ISO 12870",
    "UV400",
]

_BRIDGE_PATTERNS = [
    "Co., Ltd",
    "Co.,Ltd",
    "Co.,LTD",
    "CO.,LTD",
    "Co. Ltd",
    "Co. Ltd.",
    "Inc.",
    "Inc",
    "LLC",
    "L.L.C.",
    "GmbH",
    "AG",
    "S.A.",
    "S.A",
    "Pty Ltd",
    "Pte Ltd",
    "Ltd.",
    "Ltd",
    "LTD",
    "Limited",
    "LIMITED",
    "Corporation",
    "Corp.",
]

_CONTACT_TITLE_PATTERNS = [
    "Mr.",
    "Mr ",
    "Ms.",
    "Ms ",
    "Mrs.",
    "Mrs ",
    "Miss ",
    "Dr.",
    "Director",
    "Manager",
    "Sales Manager",
    "GM",
    "General Manager",
    "VP",
    "Vice President",
    "CEO",
    "CFO",
    "COO",
    "President",
    "Founder",
    "Owner",
    "Partner",
    "Procurement",
    "Export",
    "Sales Rep",
    "Representative",
]

# Key fields used for data_completeness_score calculation
_COMPLETENESS_FIELDS = [
    "legal_name",
    "factory_locations",
    "supplier_type",
    "supplier_subtype",
    "product_categories",
    "product_sub_categories",
    "certs_and_audits",
    "regulatory_compliance",
    "brands_worked_with",
    "contact_name",
    "market_experience",
    "certification_link",
    "moq",
    "moq_info",
    "email",
    "wechat_id",
    "qualification_score",
    "data_completeness_score",
]

# ---------------------------------------------------------------------------
# Regex helpers (compiled once)
# ---------------------------------------------------------------------------

_RE_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)
_RE_WECHAT = re.compile(
    r"(?:WeChat|wechat|Weixin|weixin|微信(?:号)?|WeChat ID|wechat id)"
    r"[\s:：=]*([a-zA-Z0-9_\-]{5,30})",
    re.IGNORECASE,
)
_RE_WEIXIN_LINK = re.compile(r"weixin\.qq\.com/([a-zA-Z0-9_\-]+)")
_RE_MOQ = re.compile(
    r"(?:MOQ|moq|min(?:imum)?\s*order)"
    r"[\s:：=]*([0-9,]+)\s*(?:pcs?|pieces?|units?|sets?|items?|pairs?)?",
    re.IGNORECASE,
)
_RE_MOQ_STANDALONE = re.compile(
    r"([0-9,]+)\s*(?:pcs?|pieces?|units?|sets?|items?|pairs?)\s+"
    r"(?:min(?:imum)?|MOQ|moq|order)",
    re.IGNORECASE,
)
_RE_YEAR_FOUNDED = re.compile(
    r"(?:established|founded|since|est\.?|incorporated|started|in business)"
    r"[\s:]*?(?:\bin\b\s*)?((?:19|20)\d{2})",
    re.IGNORECASE,
)
_RE_EXPERIENCE = re.compile(
    r"(\d{1,2})\+?\s*years?(?:\s+of\s+(?:experience|history|operation))?",
    re.IGNORECASE,
)
_RE_LEGAL_NAME = re.compile(
    r"([A-Z][a-zA-Z0-9&'\- ]{2,}?"
    r"(?:Co\.,?\s*Ltd|Inc\.?|LLC|L\.L\.C\.?|GmbH|AG|S\.A\.?|Pty\s+Ltd|"
    r"Pte\s+Ltd|Ltd\.?|Limited|Corporation|Corp\.?))",
    re.IGNORECASE,
)
_RE_BRAND_MENTION = re.compile(
    r"(?:works?\s+with|suppl(?:y|ies|ier)\s+(?:to|for)|clients?\s+include|"
    r"partner(?:s)?\s+(?:of|with)|customers?\s+include|brands?\s+include|"
    r"cooperat(?:e|ing|ion)\s+with|serv(?:e|ing)\s+(?:(?:major|leading)\s+)?"
    r"(?:brands?|companies?|retailers?|chains?))[:\s]*([^\.;]+)",
    re.IGNORECASE,
)
_RE_PHONE = re.compile(
    r"(?:tel|phone|ph|fax|mobile|cell|whatsapp)[\s:：]*"
    r"(\+?\d[\d\s\-\.]{7,}\d)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data container for scraped info
# ---------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    """Accumulated enrichment data from all sources for one supplier."""

    legal_name: Optional[str] = None
    factory_locations: Optional[str] = None
    supplier_type: Optional[str] = None
    supplier_subtype: Optional[str] = None
    product_categories: Optional[str] = None
    product_sub_categories: Optional[str] = None
    certs_and_audits: Optional[str] = None
    regulatory_compliance: Optional[str] = None
    brands_worked_with: Optional[str] = None
    contact_name: Optional[str] = None
    market_experience: Optional[str] = None
    certification_link: Optional[str] = None
    moq: Optional[str] = None
    moq_info: Optional[str] = None
    email: Optional[str] = None
    wechat_id: Optional[str] = None
    phone: Optional[str] = None
    company_description: Optional[str] = None
    founding_year: Optional[str] = None
    platform_years: Optional[int] = None
    platform_response_rate: Optional[str] = None
    platform_verified: bool = False

    # Accumulators for list-type fields (joined on merge)
    _emails: list = field(default_factory=list)
    _certs: list = field(default_factory=list)
    _categories: list = field(default_factory=list)
    _sub_categories: list = field(default_factory=list)
    _brands: list = field(default_factory=list)
    _locations: list = field(default_factory=list)
    _cert_links: list = field(default_factory=list)
    _moq_values: list = field(default_factory=list)

    # ----- merge helpers -----
    def add_email(self, addr: str) -> None:
        addr = addr.strip().rstrip(".")
        if addr and addr not in self._emails:
            self._emails.append(addr)

    def add_cert(self, cert: str) -> None:
        cert = cert.strip()
        if cert and cert not in self._certs:
            self._certs.append(cert)

    def add_category(self, cat: str) -> None:
        cat = cat.strip()
        if cat and cat not in self._categories:
            self._categories.append(cat)

    def add_sub_category(self, sub: str) -> None:
        sub = sub.strip()
        if sub and sub not in self._sub_categories:
            self._sub_categories.append(sub)

    def add_brand(self, brand: str) -> None:
        brand = brand.strip().strip(",; ")
        if brand and brand not in self._brands:
            self._brands.append(brand)

    def add_location(self, loc: str) -> None:
        loc = loc.strip()
        if loc and loc not in self._locations:
            self._locations.append(loc)

    def add_cert_link(self, link: str) -> None:
        link = link.strip()
        if link and link not in self._cert_links:
            self._cert_links.append(link)

    def add_moq(self, moq: str) -> None:
        moq = moq.strip()
        if moq and moq not in self._moq_values:
            self._moq_values.append(moq)

    def consolidate(self) -> None:
        """Merge accumulated lists into their final string fields."""
        if self._emails and not self.email:
            self.email = self._emails[0]
        if self._certs and not self.certs_and_audits:
            self.certs_and_audits = "; ".join(self._certs)
        if self._categories and not self.product_categories:
            self.product_categories = "; ".join(self._categories)
        if self._sub_categories and not self.product_sub_categories:
            self.product_sub_categories = "; ".join(self._sub_categories)
        if self._brands and not self.brands_worked_with:
            self.brands_worked_with = "; ".join(self._brands)
        if self._locations and not self.factory_locations:
            self.factory_locations = "; ".join(self._locations)
        if self._cert_links and not self.certification_link:
            self.certification_link = self._cert_links[0]
        if self._moq_values and not self.moq:
            self.moq = self._moq_values[0]
            self.moq_info = "; ".join(self._moq_values)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _extract_emails(text: str) -> list[str]:
    """Return all email addresses found in *text*."""
    return _RE_EMAIL.findall(text)


def _extract_wechat(text: str) -> list[str]:
    """Return WeChat IDs found in *text*."""
    ids = _RE_WECHAT.findall(text)
    ids.extend(_RE_WEIXIN_LINK.findall(text))
    return list(dict.fromkeys(ids))  # dedupe preserving order


def _extract_legal_names(text: str) -> list[str]:
    """Return potential legal entity names found in *text*."""
    names = _RE_LEGAL_NAME.findall(text)
    # Dedupe and strip; filter out nav text and too-long matches
    nav_words = {"home", "about", "contact", "products", "services", "blog",
                 "faq", "login", "cart", "menu", "search", "newsletter",
                 "facebook", "instagram", "twitter", "linkedin", "youtube"}
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        n = n.strip()
        # Skip if too long (likely nav text, not a company name)
        if len(n) > 80:
            continue
        # Skip if first 3 words are all nav words
        words = n.split()[:3]
        if all(w.lower() in nav_words for w in words):
            continue
        if n.lower() not in seen:
            seen.add(n.lower())
            result.append(n)
    return result


def _extract_moq(text: str) -> list[str]:
    """Return MOQ values found in *text*."""
    found = _RE_MOQ.findall(text)
    found.extend(_RE_MOQ_STANDALONE.findall(text))
    return list(dict.fromkeys(found))


def _extract_founding_year(text: str) -> Optional[str]:
    """Return first plausible founding year found in *text*."""
    m = _RE_YEAR_FOUNDED.search(text)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2030:
            return str(year)
    return None


def _extract_experience_years(text: str) -> Optional[str]:
    """Return experience in years if found in *text*."""
    m = _RE_EXPERIENCE.search(text)
    if m:
        return f"{m.group(1)} years"
    return None


def _extract_cert_keywords(text: str) -> list[str]:
    """Return cert standard names mentioned in *text*."""
    found: list[str] = []
    for kw in _CERT_KEYWORDS:
        if kw.lower() in text.lower():
            if kw not in found:
                found.append(kw)
    return found


def _extract_brand_mentions(text: str) -> list[str]:
    """Return brand/client mentions from *text*."""
    found: list[str] = []
    for m in _RE_BRAND_MENTION.finditer(text):
        mention = m.group(1).strip().rstrip(".,; ")
        if mention and len(mention) < 200:
            # Split on commas/and to get individual brand names
            parts = re.split(r",|\band\b", mention)
            for p in parts:
                p = p.strip().rstrip(".,; ")
                # Skip non-brand noise (sentences, marketing copy)
                if not p or len(p) > 60:
                    continue
                # Skip if it looks like a sentence (has common English filler words)
                common = {"whether", "launching", "scaling", "sourcing", "volume",
                          "model", "that", "this", "have", "with", "from", "your"}
                words = p.lower().split()
                common_count = sum(1 for w in words if w in common)
                if common_count >= 2:
                    continue
                if p not in found:
                    found.append(p)
    return found


def _extract_contact_person(text: str) -> Optional[str]:
    """Return a contact person name found in *text*."""
    for pattern in _CONTACT_TITLE_PATTERNS:
        idx = text.find(pattern)
        if idx != -1:
            # Grab text after the title up to next newline or punctuation
            after = text[idx + len(pattern) : idx + len(pattern) + 60]
            # Try to extract a name (letters, spaces, dots, hyphens) — must end at punctuation
            name_match = re.match(r"[\s:：]*([A-Z][a-zA-Z\s\.\-]{1,25}?)(?:[,;\n|]|$)", after)
            if not name_match:
                # Broader match but validate it looks like a person name
                name_match = re.match(r"[\s:：]*([A-Z][a-z]+\s+[A-Z][a-z]+)", after)
            if name_match:
                name = name_match.group(1).strip()
                # Validate: must be 2-30 chars, no nav words, look like a name
                if 2 <= len(name) <= 30 and not any(w in name.lower() for w in ("home", "about", "product", "service", "blog")):
                    return f"{pattern} {name}".strip()
    return None


def _extract_phones(text: str) -> list[str]:
    """Return phone numbers found in *text*."""
    return _RE_PHONE.findall(text)


def _extract_supplier_type(text: str) -> Optional[str]:
    """Infer supplier type from text (Manufacturer vs Trading Company)."""
    t = text.lower()
    if "manufacturer" in t and "trading" in t:
        return "Manufacturer/Trading Company"
    if "manufacturer" in t:
        return "Manufacturer"
    if "trading company" in t or "trading co" in t:
        return "Trading Company"
    if "trader" in t:
        return "Trading Company"
    return None


def _extract_product_categories_from_text(text: str) -> tuple[list[str], list[str]]:
    """Return (categories, sub_categories) extracted from product-related text."""
    categories: list[str] = []
    sub_categories: list[str] = []

    t = text.lower()
    category_keywords = {
        "Towel": ["towel"],
        "Bath Towel": ["bath towel"],
        "Hand Towel": ["hand towel"],
        "Face Towel": ["face towel", "washcloth", "face cloth"],
        "Beach Towel": ["beach towel"],
        "Kitchen Towel": ["kitchen towel", "tea towel"],
        "Golf Towel": ["golf towel"],
        "Sports Towel": ["sports towel"],
        "Hotel Towel": ["hotel towel", "hospitality towel"],
        "Bathrobe": ["bathrobe", "bath robe", "robe"],
        "Bath Mat": ["bath mat", "bathmat"],
        "Bed Linen": ["bed linen", "bed sheet", "bedding", "duvet", "pillowcase"],
        "Blanket": ["blanket", "throw"],
        "Cushion": ["cushion", "pillow"],
        "Fabric": ["fabric", "textile", "cloth"],
        "Eyewear": ["eyewear", "optical", "sunglasses", "glasses", "spectacles", "frames"],
        "Sunglasses": ["sunglasses", "sun glasses", "shades"],
        "Optical Frames": ["optical frame", "eyeglass frame", "eyeglasses"],
        "Skincare": ["skincare", "skin care", "moisturizer", "cream", "lotion", "serum"],
        "Cosmetics": ["cosmetics", "makeup", "beauty"],
        "Candle": ["candle", "soy candle", "wax candle"],
        "Jewelry": ["jewelry", "jewellery", "ring", "necklace", "bracelet"],
        "Watch": ["watch", "timepiece", "horology"],
        "Apparel": ["apparel", "clothing", "garment", "fashion"],
        "Footwear": ["footwear", "shoes", "boots", "sneakers"],
        "Bags": ["bags", "handbag", "backpack", "luggage"],
    }
    for cat, kws in category_keywords.items():
        for kw in kws:
            if kw in t and cat not in categories:
                categories.append(cat)
                break

    sub_keywords = {
        "Microfiber": ["microfiber"],
        "Cotton": ["cotton"],
        "Organic Cotton": ["organic cotton"],
        "Bamboo": ["bamboo"],
        "Linen": ["linen"],
        "Jacquard": ["jacquard"],
        "Terry": ["terry"],
        "Velour": ["velour"],
        "Waffle": ["waffle"],
        "Printed": ["printed", "printing"],
        "Embroidered": ["embroidered", "embroidery"],
        "Yarn-Dyed": ["yarn-dyed", "yarn dyed"],
        "Combed Cotton": ["combed"],
        "Zero-Twist": ["zero-twist", "zero twist"],
        "Ring Spun": ["ring spun", "ring-spun"],
        "Acetate": ["acetate"],
        "Titanium": ["titanium"],
        "Stainless Steel": ["stainless steel", "steel frame"],
        "TR90": ["tr90", "tr-90"],
        "Polarized": ["polarized"],
        "UV Protection": ["uv protection", "uv400", "uv 400"],
        "Acetate Frame": ["acetate frame"],
        "Metal Frame": ["metal frame"],
        "Injection": ["injection", "injection mold"],
        "Natural/Organic": ["natural", "organic", "plant-based"],
        "Tallow-Based": ["tallow", "beef tallow"],
        "Whipped": ["whipped", "whip"],
    }
    for sub, kws in sub_keywords.items():
        for kw in kws:
            if kw in t and sub not in sub_categories:
                sub_categories.append(sub)
                break

    return categories, sub_categories


# ---------------------------------------------------------------------------
# Page scraper
# ---------------------------------------------------------------------------

def _fetch_page(client: httpx.Client, url: str) -> Optional[tuple[str, str]]:
    """Fetch *url* and return (final_url, html_text) or None on failure."""
    try:
        resp = client.get(url, headers={"User-Agent": _USER_AGENT})
        if resp.status_code < 400:
            return str(resp.url), resp.text
    except (httpx.HTTPError, httpx.StreamError, Exception) as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
    return None


def _scrape_homepage(base_url: str, client: httpx.Client, sr: ScrapeResult) -> None:
    """Scrape homepage and extract company description, certs in text."""
    result = _fetch_page(client, base_url)
    if not result:
        return
    _, html = result
    soup = BeautifulSoup(html, "html.parser")

    # Company description from meta or first big paragraph
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        sr.company_description = meta_desc["content"].strip()[:500]

    text = soup.get_text(separator=" ", strip=True)
    if not sr.company_description or len(sr.company_description) < 30:
        # Try first prominent paragraph
        for p in soup.find_all("p"):
            ptext = p.get_text(strip=True)
            if 50 < len(ptext) < 600:
                sr.company_description = ptext[:500]
                break

    # Extract certs mentioned on homepage
    for cert in _extract_cert_keywords(text):
        sr.add_cert(cert)

    # Extract product categories
    cats, subs = _extract_product_categories_from_text(text)
    for c in cats:
        sr.add_category(c)
    for s in subs:
        sr.add_sub_category(s)

    # Emails, WeChat, legal names
    for email in _extract_emails(text):
        sr.add_email(email)
    for wc in _extract_wechat(text):
        sr.wechat_id = sr.wechat_id or wc
    for name in _extract_legal_names(text):
        sr.legal_name = sr.legal_name or name
    for brand in _extract_brand_mentions(text):
        sr.add_brand(brand)

    # Supplier type
    stype = _extract_supplier_type(text)
    if stype:
        sr.supplier_type = sr.supplier_type or stype


def _scrape_about_page(base_url: str, client: httpx.Client, sr: ScrapeResult) -> None:
    """Scrape about/company pages for legal name, founding year, experience, contact people."""
    for _, path in _CRAWL_PATHS:
        if path in ("/about", "/about-us", "/company", "/company-profile"):
            url = urljoin(base_url, path)
            result = _fetch_page(client, url)
            if not result:
                continue
            _, html = result
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Legal name
            for name in _extract_legal_names(text):
                sr.legal_name = sr.legal_name or name

            # Founding year / experience
            year = _extract_founding_year(text)
            if year:
                sr.founding_year = year
                sr.market_experience = sr.market_experience or f"Since {year}"

            exp = _extract_experience_years(text)
            if exp:
                sr.market_experience = sr.market_experience or exp

            # Contact person
            contact = _extract_contact_person(text)
            if contact:
                sr.contact_name = sr.contact_name or contact

            # Supplier type
            stype = _extract_supplier_type(text)
            if stype:
                sr.supplier_type = sr.supplier_type or stype

            # Factory locations — look for address patterns
            for loc in _extract_locations_from_text(text):
                sr.add_location(loc)

            # Any emails that appear on about pages
            for email in _extract_emails(text):
                sr.add_email(email)

            # Certs
            for cert in _extract_cert_keywords(text):
                sr.add_cert(cert)


def _extract_locations_from_text(text: str) -> list[str]:
    """Try to extract factory/city/province location mentions."""
    locations: list[str] = []
    # Common Chinese province/city patterns
    location_pattern = re.compile(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s*"
        r"(?:Province|City|District|County|Town|Village|Industrial(?:\s+Zone|Area|Park)))",
        re.IGNORECASE,
    )
    for m in location_pattern.finditer(text):
        loc = m.group(0).strip()
        if loc not in locations:
            locations.append(loc)
    # Also try "City, Province, China" patterns
    china_pattern = re.compile(
        r"([A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*China)",
    )
    for m in china_pattern.finditer(text):
        loc = m.group(0).strip()
        if loc not in locations:
            locations.append(loc)
    return locations


def _scrape_contact_page(base_url: str, client: httpx.Client, sr: ScrapeResult) -> None:
    """Scrape contact pages for email, phone, WeChat."""
    for _, path in _CRAWL_PATHS:
        if path in ("/contact", "/contact-us"):
            url = urljoin(base_url, path)
            result = _fetch_page(client, url)
            if not result:
                continue
            _, html = result
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Emails
            for email in _extract_emails(text):
                sr.add_email(email)

            # Also check mailto: links
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip()
                    sr.add_email(addr)

            # WeChat IDs
            for wc in _extract_wechat(text):
                sr.wechat_id = sr.wechat_id or wc

            # Look for WeChat in links
            for a in soup.find_all("a", href=True):
                m = _RE_WEIXIN_LINK.search(a["href"])
                if m:
                    sr.wechat_id = sr.wechat_id or m.group(1)

            # Phone numbers
            for phone in _extract_phones(text):
                sr.phone = sr.phone or phone

            # Contact person
            contact = _extract_contact_person(text)
            if contact:
                sr.contact_name = sr.contact_name or contact

            # Location from address blocks
            for loc in _extract_locations_from_text(text):
                sr.add_location(loc)


def _scrape_products_page(base_url: str, client: httpx.Client, sr: ScrapeResult) -> None:
    """Scrape product/catalog pages for categories and MOQ."""
    for _, path in _CRAWL_PATHS:
        if path in ("/products", "/product", "/catalog"):
            url = urljoin(base_url, path)
            result = _fetch_page(client, url)
            if not result:
                continue
            _, html = result
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Product categories
            cats, subs = _extract_product_categories_from_text(text)
            for c in cats:
                sr.add_category(c)
            for s in subs:
                sr.add_sub_category(s)

            # MOQ
            for moq in _extract_moq(text):
                sr.add_moq(moq)

            # Also try product links for category names
            for a in soup.find_all("a", href=True):
                link_text = a.get_text(strip=True)
                if link_text and 3 < len(link_text) < 60:
                    lc_cats, lc_subs = _extract_product_categories_from_text(link_text)
                    for c in lc_cats:
                        sr.add_category(c)
                    for s in lc_subs:
                        sr.add_sub_category(s)


def _scrape_certifications_page(base_url: str, client: httpx.Client, sr: ScrapeResult) -> None:
    """Scrape certification pages for cert details and links."""
    for _, path in _CRAWL_PATHS:
        if path in ("/certificates", "/certifications", "/quality"):
            url = urljoin(base_url, path)
            result = _fetch_page(client, url)
            if not result:
                continue
            final_url, html = result
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Cert keywords
            for cert in _extract_cert_keywords(text):
                sr.add_cert(cert)

            # Cert links (images or PDFs referencing certificates)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                link_text = a.get_text(strip=True).lower()
                if any(kw.lower() in link_text for kw in _CERT_KEYWORDS):
                    full = urljoin(final_url, href)
                    sr.add_cert_link(full)
                # Also check href for cert-related paths
                if any(kw in href.lower() for kw in ("cert", "audit", "quality", "iso", "oeko")):
                    full = urljoin(final_url, href)
                    sr.add_cert_link(full)

            # Cert images (often show cert badges)
            for img in soup.find_all("img", src=True):
                alt = (img.get("alt") or "").lower()
                src = img["src"].lower()
                if any(kw.lower() in alt for kw in _CERT_KEYWORDS) or \
                   any(kw in src for kw in ("cert", "oeko", "iso", "gots", "grs")):
                    full = urljoin(final_url, img["src"])
                    sr.add_cert_link(full)


# ---------------------------------------------------------------------------
# Alibaba / Made-in-China cross-reference
# ---------------------------------------------------------------------------

def _cross_reference_alibaba(trade_name: str, sr: ScrapeResult) -> None:
    """Search Alibaba for the supplier and scrape their storefront."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(
                f'site:alibaba.com "{trade_name}"',
                max_results=5,
            ))
    except Exception as exc:
        logger.warning("DDG search for Alibaba '%s' failed: %s", trade_name, exc)
        return

    if not results:
        return

    alibaba_urls = [r["href"] for r in results if "alibaba.com" in (r.get("href") or "")]
    if not alibaba_urls:
        return

    # Try the first relevant URL
    for url in alibaba_urls[:2]:
        _scrape_alibaba_storefront(url, sr)


def _scrape_alibaba_storefront(url: str, sr: ScrapeResult) -> None:
    """Scrape an Alibaba company profile / storefront page."""
    try:
        with httpx.Client(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            result = _fetch_page(client, url)
            if not result:
                return
            final_url, html = result
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Legal company name — Alibaba always shows this
            for name in _extract_legal_names(text):
                sr.legal_name = sr.legal_name or name

            # Business type
            stype = _extract_supplier_type(text)
            if stype:
                sr.supplier_type = sr.supplier_type or stype

            # Location
            # Alibaba pattern: "City, Province, China"
            location_match = re.search(
                r"([A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*China)", text
            )
            if location_match:
                sr.add_location(location_match.group(1))

            for loc in _extract_locations_from_text(text):
                sr.add_location(loc)

            # Years on platform
            years_match = re.search(r"(\d+)\s*\+?\s*Years?", text)
            if years_match:
                sr.platform_years = int(years_match.group(1))
                if not sr.market_experience:
                    sr.market_experience = f"{years_match.group(1)} years on Alibaba"

            # Response rate
            resp_match = re.search(r"(\d+\.?\d*%)\s*Response\s*Rate", text, re.IGNORECASE)
            if resp_match:
                sr.platform_response_rate = resp_match.group(1)

            # Verified supplier?
            if "verified supplier" in text.lower() or "verified" in text.lower():
                sr.platform_verified = True
                sr.add_cert("Alibaba Verified")

            # Certifications listed
            for cert in _extract_cert_keywords(text):
                sr.add_cert(cert)

            # Main products
            cats, subs = _extract_product_categories_from_text(text)
            for c in cats:
                sr.add_category(c)
            for s in subs:
                sr.add_sub_category(s)

            # MOQ
            for moq in _extract_moq(text):
                sr.add_moq(moq)

            # Emails
            for email in _extract_emails(text):
                sr.add_email(email)

            # Export percentage
            export_match = re.search(r"(\d+\.?\d*%)\s*Export", text, re.IGNORECASE)
            if export_match and not sr.regulatory_compliance:
                sr.regulatory_compliance = f"Export ratio: {export_match.group(1)}"

            # Revenue
            revenue_match = re.search(
                r"(?:Total\s+Revenue|Annual\s+Revenue)[\s:]*USD\s*([\d,.]+\s*(?:Million|Thousand|Billion)?)",
                text,
                re.IGNORECASE,
            )
            if revenue_match:
                sr.supplier_subtype = sr.supplier_subtype or f"Revenue: USD {revenue_match.group(1)}"

    except Exception as exc:
        logger.warning("Alibaba storefront scrape failed for %s: %s", url, exc)


def _cross_reference_mic(trade_name: str, sr: ScrapeResult) -> None:
    """Search Made-in-China for the supplier and scrape their page."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(
                f'site:made-in-china.com "{trade_name}"',
                max_results=5,
            ))
    except Exception as exc:
        logger.warning("DDG search for MIC '%s' failed: %s", trade_name, exc)
        return

    if not results:
        return

    mic_urls = [r["href"] for r in results if "made-in-china.com" in (r.get("href") or "")]
    if not mic_urls:
        return

    for url in mic_urls[:2]:
        _scrape_mic_storefront(url, sr)


def _scrape_mic_storefront(url: str, sr: ScrapeResult) -> None:
    """Scrape a Made-in-China company profile page."""
    try:
        with httpx.Client(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            result = _fetch_page(client, url)
            if not result:
                return
            final_url, html = result
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)

            # Legal name
            for name in _extract_legal_names(text):
                sr.legal_name = sr.legal_name or name

            # Business type
            stype = _extract_supplier_type(text)
            if stype:
                sr.supplier_type = sr.supplier_type or stype

            # Location
            location_match = re.search(
                r"([A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*China)", text
            )
            if location_match:
                sr.add_location(location_match.group(1))
            for loc in _extract_locations_from_text(text):
                sr.add_location(loc)

            # Years on platform
            years_match = re.search(r"(\d+)\s*\+?\s*Years?", text)
            if years_match:
                if not sr.platform_years:
                    sr.platform_years = int(years_match.group(1))
                if not sr.market_experience:
                    sr.market_experience = f"{years_match.group(1)} years on Made-in-China"

            # Certifications
            for cert in _extract_cert_keywords(text):
                sr.add_cert(cert)

            # Products
            cats, subs = _extract_product_categories_from_text(text)
            for c in cats:
                sr.add_category(c)
            for s in subs:
                sr.add_sub_category(s)

            # MOQ
            for moq in _extract_moq(text):
                sr.add_moq(moq)

            # Brand mentions
            for brand in _extract_brand_mentions(text):
                sr.add_brand(brand)

            # Contact person
            contact = _extract_contact_person(text)
            if contact:
                sr.contact_name = sr.contact_name or contact

            if "verified" in text.lower():
                sr.platform_verified = True
                sr.add_cert("Made-in-China Verified")

    except Exception as exc:
        logger.warning("MIC storefront scrape failed for %s: %s", url, exc)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _calc_qualification_score(sr: ScrapeResult) -> int:
    """
    Calculate qualification score 0-100.

    Breakdown:
      Certifications:        25 pts max
      Brands worked with:    20 pts max
      Data completeness:     20 pts max
      Platform verification: 15 pts max
      Experience:            10 pts max
      MOQ clarity:           10 pts max
    """
    score = 0

    # Certifications (25)
    cert_count = len(sr._certs)
    if cert_count >= 5:
        score += 25
    elif cert_count >= 3:
        score += 18
    elif cert_count >= 2:
        score += 12
    elif cert_count >= 1:
        score += 6

    # Brands worked with (20)
    brand_count = len(sr._brands)
    if brand_count >= 5:
        score += 20
    elif brand_count >= 3:
        score += 14
    elif brand_count >= 2:
        score += 10
    elif brand_count >= 1:
        score += 5

    # Data completeness (20) — proportional to completeness score
    completeness = _calc_completeness_score(sr)
    score += int(20 * completeness / 100)

    # Platform verification (15)
    if sr.platform_verified:
        score += 15

    # Experience (10)
    if sr.market_experience:
        exp_match = re.search(r"(\d+)", sr.market_experience)
        if exp_match:
            years = int(exp_match.group(1))
            if years >= 20:
                score += 10
            elif years >= 10:
                score += 7
            elif years >= 5:
                score += 5
            elif years >= 2:
                score += 3
        else:
            score += 3  # Some experience info exists
    if sr.platform_years and sr.platform_years >= 5:
        score = min(score + 3, 100)

    # MOQ clarity (10)
    if sr.moq:
        score += 10
    elif sr.moq_info:
        score += 5

    return min(score, 100)


def _calc_completeness_score(sr: ScrapeResult) -> int:
    """
    Calculate data completeness score 0-100.

    Based on % of key fields that are filled.
    """
    filled = 0
    total = len(_COMPLETENESS_FIELDS)

    field_map = {
        "legal_name": sr.legal_name,
        "factory_locations": sr.factory_locations,
        "supplier_type": sr.supplier_type,
        "supplier_subtype": sr.supplier_subtype,
        "product_categories": sr.product_categories,
        "product_sub_categories": sr.product_sub_categories,
        "certs_and_audits": sr.certs_and_audits,
        "regulatory_compliance": sr.regulatory_compliance,
        "brands_worked_with": sr.brands_worked_with,
        "contact_name": sr.contact_name,
        "market_experience": sr.market_experience,
        "certification_link": sr.certification_link,
        "moq": sr.moq,
        "moq_info": sr.moq_info,
        "email": sr.email,
        "wechat_id": sr.wechat_id,
        # These will be filled later; count them as 0 for now
        "qualification_score": None,
        "data_completeness_score": None,
    }

    for fname, fval in field_map.items():
        if fval is not None and str(fval).strip():
            filled += 1

    # Subtract 2 from total for the score fields themselves (they're computed)
    effective_total = total - 2
    if effective_total <= 0:
        return 0

    return int(100 * filled / effective_total)


# ---------------------------------------------------------------------------
# Core enrichment pipeline
# ---------------------------------------------------------------------------

def _enrich_one(supplier: dict) -> dict:
    """Deep-enrich a single supplier record."""
    trade_name = supplier.get("trade_name") or supplier.get("legal_name") or ""
    website = supplier.get("website") or supplier.get("url") or ""

    sr = ScrapeResult()
    pages_scraped = 0

    # --- Phase 1: Multi-page scrape of supplier's own website ---
    if website:
        # Normalize base URL
        base_url = website.strip()
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        parsed = urlparse(base_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        logger.info("Scraping website for '%s': %s", trade_name, base_url)

        with httpx.Client(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            # Homepage
            _scrape_homepage(base_url, client, sr)
            pages_scraped += 1
            time.sleep(0.3)

            # About pages
            if pages_scraped < _MAX_PAGES_PER_SUPPLIER:
                _scrape_about_page(base_url, client, sr)
                pages_scraped += 1
                time.sleep(0.3)

            # Contact pages
            if pages_scraped < _MAX_PAGES_PER_SUPPLIER:
                _scrape_contact_page(base_url, client, sr)
                pages_scraped += 1
                time.sleep(0.3)

            # Product pages
            if pages_scraped < _MAX_PAGES_PER_SUPPLIER:
                _scrape_products_page(base_url, client, sr)
                pages_scraped += 1
                time.sleep(0.3)

            # Certification pages
            if pages_scraped < _MAX_PAGES_PER_SUPPLIER:
                _scrape_certifications_page(base_url, client, sr)
                pages_scraped += 1
                time.sleep(0.3)

        logger.info(
            "Website scrape complete for '%s': %d pages scraped",
            trade_name,
            pages_scraped,
        )

    # --- Phase 2: Alibaba cross-reference ---
    if trade_name:
        logger.info("Cross-referencing Alibaba for '%s'", trade_name)
        _cross_reference_alibaba(trade_name, sr)
        time.sleep(0.5)

    # --- Phase 3: Made-in-China cross-reference ---
    if trade_name:
        logger.info("Cross-referencing Made-in-China for '%s'", trade_name)
        _cross_reference_mic(trade_name, sr)
        time.sleep(0.5)

    # --- Consolidate accumulated data ---
    sr.consolidate()

    # --- Compute scores ---
    sr.qualification_score = _calc_qualification_score(sr)
    sr.data_completeness_score = _calc_completeness_score(sr)

    # --- Merge into supplier dict (don't overwrite existing data) ---
    enrich_fields = {
        "legal_name": sr.legal_name,
        "factory_locations": sr.factory_locations,
        "supplier_type": sr.supplier_type,
        "supplier_subtype": sr.supplier_subtype,
        "product_categories": sr.product_categories,
        "product_sub_categories": sr.product_sub_categories,
        "certs_and_audits": sr.certs_and_audits,
        "regulatory_compliance": sr.regulatory_compliance,
        "brands_worked_with": sr.brands_worked_with,
        "contact_name": sr.contact_name,
        "market_experience": sr.market_experience,
        "certification_link": sr.certification_link,
        "moq": sr.moq,
        "moq_info": sr.moq_info,
        "email": sr.email,
        "wechat_id": sr.wechat_id,
        "qualification_score": sr.qualification_score,
        "data_completeness_score": sr.data_completeness_score,
    }

    for key, value in enrich_fields.items():
        if value is not None and str(value).strip():
            existing = supplier.get(key)
            # Don't overwrite if the field already has data
            if existing is None or not str(existing).strip():
                supplier[key] = value

    # Set outreach state
    supplier["outreach_state"] = "ENRICHED"

    logger.info(
        "Enrichment complete for '%s': qualification=%d, completeness=%d",
        trade_name,
        sr.qualification_score,
        sr.data_completeness_score,
    )

    return supplier


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_suppliers(suppliers: list[dict]) -> list[dict]:
    """
    Deep-enrich a list of supplier dicts via multi-source research.

    For each supplier the agent:
      1. Multi-page scrapes the supplier's own website
      2. Cross-references on Alibaba.com and Made-in-China.com
      3. Extracts structured data (legal name, certs, MOQ, contacts, etc.)
      4. Computes qualification and completeness scores
      5. Sets outreach_state = 'ENRICHED'

    Existing fields with data are never overwritten.

    Args:
        suppliers: List of supplier dicts (must have at least 'trade_name'
                   and optionally 'website'/'url').

    Returns:
        List of enriched supplier dicts (same objects, mutated in-place).
    """
    logger.info("Starting enrichment for %d suppliers", len(suppliers))
    enriched = []
    for i, supplier in enumerate(suppliers, 1):
        name = supplier.get("trade_name") or supplier.get("legal_name") or f"#{i}"
        logger.info("Enriching supplier %d/%d: %s", i, len(suppliers), name)
        try:
            result = _enrich_one(supplier)
            enriched.append(result)
        except Exception as exc:
            logger.error(
                "Enrichment failed for '%s': %s", name, exc, exc_info=True
            )
            # Still include the supplier, just mark it
            supplier.setdefault("outreach_state", "ENRICHMENT_FAILED")
            enriched.append(supplier)

    logger.info(
        "Enrichment batch complete: %d/%d succeeded",
        sum(1 for s in enriched if s.get("outreach_state") == "ENRICHED"),
        len(enriched),
    )
    return enriched
