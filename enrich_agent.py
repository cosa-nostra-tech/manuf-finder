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

# Words that indicate a legal-name match is NOT a real company name
_LEGAL_NAME_STOPWORDS = frozenset({
    "about", "quality", "service", "services", "advantage", "advantages",
    "product", "products", "solution", "solutions", "technology", "innovation",
    "contact", "home", "blog", "news", "faq", "career", "careers",
    "resource", "resources", "support", "help", "learn", "discover",
    "welcome", "introduction", "overview", "history", "mission", "vision",
    "value", "packaging", "package", "packag", "assurance", "certificate",
    "certification", "past", "present", "future", "glasses", "sunglasses",
})

# Regex to detect a company suffix within a word/phrase
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:Co\.,?\s*L(?:td|TD)|Inc\.?|LLC|L\.L\.C\.?|GmbH|AG|S\.A\.?|"
    r"Pty\s+Ltd|Pte\s+Ltd|Ltd\.?|LTD|Limited|LIMITED|Corporation|Corp\.?)\b"
)

# Trailing words that end a contact-person name match
_CONTACT_TRAILING_STOPWORDS = frozenset({
    "address", "email", "phone", "tel", "fax", "wechat", "whatsapp",
    "mobile", "cell", "skype", "linkedin", "website", "web", "http",
    "position", "title", "dept", "department",
})

# Stopwords for fake "City" matches in location extraction
_LOCATION_CITY_STOPWORDS = frozenset({
    "machine", "chiller", "pellet", "bag", "bagging", "mill", "press",
    "pump", "compressor", "generator", "motor", "engine", "turbine",
    "crusher", "grinder", "mixer", "blender", "dryer", "heater",
    "cooler", "filter", "conveyor", "elevator", "crane", "forklift",
    "welding", "cutting", "drilling", "milling", "lathe", "cnc",
    "injection", "extrusion", "molding", "stamping", "casting",
    "forging", "plating", "coating", "painting", "printing",
    "embroidery", "knitting", "weaving", "spinning", "dyeing",
    "finishing", "washing", "ironing", "sewing", "stitching",
    "packing", "labeling", "testing", "inspection", "sorting",
    "air", "water", "oil", "gas", "steam", "hot", "cold", "big",
})

# Whitelist of ~50 most common Chinese manufacturing cities and provinces
_CHINESE_LOCATIONS = frozenset({
    "Guangdong", "Zhejiang", "Jiangsu", "Shandong", "Fujian", "Hebei",
    "Henan", "Hubei", "Hunan", "Anhui", "Sichuan", "Liaoning",
    "Shaanxi", "Jiangxi", "Guangxi", "Guizhou", "Yunnan", "Shanxi",
    "Heilongjiang", "Jilin", "Gansu", "Inner Mongolia", "Hainan",
    "Ningxia", "Qinghai", "Tibet", "Xinjiang",
    "Guangzhou", "Shenzhen", "Dongguan", "Foshan", "Shantou",
    "Zhongshan", "Zhuhai", "Huizhou", "Jiangmen", "Shunde",
    "Yiwu", "Hangzhou", "Ningbo", "Wenzhou", "Shaoxing",
    "Jiaxing", "Taizhou", "Jinhua", "Huzhou", "Cixi",
    "Nanjing", "Suzhou", "Wuxi", "Changzhou", "Nantong",
    "Yangzhou", "Zhenjiang", "Xuzhou", "Kunshan", "Changshu",
    "Qingdao", "Jinan", "Weifang", "Yantai", "Linyi",
    "Xiamen", "Quanzhou", "Fuzhou", "Zhangzhou", "Putian",
    "Shanghai", "Beijing", "Tianjin", "Chongqing",
    "Chengdu", "Wuhan", "Changsha", "Zhengzhou", "Hefei",
    "Shijiazhuang", "Baoding", "Tangshan",
})

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
    r"(?:MOQ|moq|min(?:imum)?\.?\s*order|start\s*order)"
    r"[\s:：=]*([0-9,]+)\s*(?:pcs?|pieces?|units?|sets?|items?|pairs?)?",
    re.IGNORECASE,
)
_RE_MOQ_STANDALONE = re.compile(
    r"([0-9,]+)\s*(?:pcs?|pieces?|units?|sets?|items?|pairs?)\s+"
    r"(?:min(?:imum)?|MOQ|moq|order)",
    re.IGNORECASE,
)
_RE_MOQ_PAREN = re.compile(
    r"([0-9,]+)\s*(?:pcs?|pieces?|units?|sets?|items?|pairs?)"
    r"\s*\(\s*(?:Min\.?\s*Order|MOQ|Minimum\s*Order)\s*\)",
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
    r"([A-Z][a-zA-Z0-9&'\-]+\s+[A-Z][a-zA-Z0-9&'\-]+"
    r"(?:\s+[A-Z][a-zA-Z0-9&'\-]+)*\s+"
    r"(?:Co\.,?\s*L(?:td|TD)|Inc\.?|LLC|L\.L\.C\.?|GmbH|AG|S\.A\.?|"
    r"Pty\s+Ltd|Pte\s+Ltd|Ltd\.?|LTD|Limited|LIMITED|Corporation|Corp\.?))\b"
)
_RE_BRAND_MENTION = re.compile(
    r"(?:brands?\s+including|clients?\s+include|customers?\s+include|"
    r"suppl(?:y|ies|ied)\s+to|supplied\s+to|partner(?:s)?\s+of|"
    r"brands?\s+include)"
    r"[:\s]*([^\.;]+)",
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


def _extract_legal_names(soup: BeautifulSoup) -> list[str]:
    """Return potential legal entity names found in targeted HTML elements.

    Only searches <title>, <h1>, <footer>, and elements with class/id
    containing company/legal/copyright/about/footer keywords — avoids
    matching random body text like nav bars or marketing paragraphs.
    Requires a company suffix (Ltd, GmbH, etc.), at least 2 words before
    the suffix, and no stop-words.
    """
    targeted_texts: list[str] = []

    # 1. <title> tag
    title_tag = soup.find("title")
    if title_tag:
        t = title_tag.get_text(strip=True)
        if t:
            targeted_texts.append(t)

    # 2. <h1> tags
    for h1 in soup.find_all("h1"):
        t = h1.get_text(separator=" ", strip=True)
        if t:
            targeted_texts.append(t)

    # 3. <footer> tags
    for footer in soup.find_all("footer"):
        t = footer.get_text(separator=" ", strip=True)
        if t:
            targeted_texts.append(t)

    # 4. Elements with class/id containing company-related keywords
    company_kw = ("company", "legal", "copyright", "about", "footer",
                  "corp", "business", "org")
    for tag in soup.find_all(True):
        cls_str = " ".join(tag.get("class", []))
        tag_id = tag.get("id", "") or ""
        ident = (cls_str + " " + tag_id).lower()
        if any(kw in ident for kw in company_kw):
            t = tag.get_text(separator=" ", strip=True)
            if t and len(t) < 300:
                targeted_texts.append(t)

    combined = " ".join(targeted_texts)
    if not combined:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for m in _RE_LEGAL_NAME.finditer(combined):
        name = m.group(1).strip()
        if len(name) > 80:
            continue
        words = name.split()
        # Count words before the company suffix
        prefix_count = 0
        for w in words:
            if _COMPANY_SUFFIX_RE.search(w):
                break
            prefix_count += 1
        if prefix_count < 2:
            continue
        # Reject if any word is a stopword
        if any(w.lower() in _LEGAL_NAME_STOPWORDS for w in words):
            continue
        if name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def _extract_moq(text: str) -> list[str]:
    """Return MOQ values found in *text*."""
    found = _RE_MOQ.findall(text)
    found.extend(_RE_MOQ_STANDALONE.findall(text))
    found.extend(_RE_MOQ_PAREN.findall(text))
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
    """Return brand/client mentions from *text*.

    Only accepts brands mentioned in specific contexts
    ("brands including", "clients include", "supplied to", etc.).
    Rejects mentions containing marketing verbs.  Each individual brand
    name must be ≤3 words and start with a capital letter.
    """
    _BRAND_MARKETING_VERBS = frozenset({
        "develop", "create", "help", "assist", "provide", "offer",
        "ensure", "deliver", "improve", "enhance", "support", "enable",
        "produce", "manufacture", "customize", "design", "build",
    })
    found: list[str] = []
    for m in _RE_BRAND_MENTION.finditer(text):
        mention = m.group(1).strip().rstrip(".,; ")
        if not mention or len(mention) > 200:
            continue
        # Reject the entire mention if it contains marketing verbs
        mention_lower = mention.lower()
        if any(f" {v} " in f" {mention_lower} " for v in _BRAND_MARKETING_VERBS):
            continue
        # Split on commas/and to get individual brand names
        parts = re.split(r",|\band\b", mention)
        for p in parts:
            p = p.strip().rstrip(".,; ")
            if not p:
                continue
            words = p.split()
            # Each brand name ≤3 words, first word starts with capital
            if len(words) > 3:
                continue
            if not words[0][0].isupper():
                continue
            if p not in found:
                found.append(p)
    return found


def _extract_contact_person(text: str) -> Optional[str]:
    """Return a contact person name found in *text*.

    Stops name match at common trailing label words (Address, Email, etc.).
    Only accepts if the name part (after the title) has ≤3 words.
    Strips any trailing non-name words.
    """
    for pattern in _CONTACT_TITLE_PATTERNS:
        idx = text.find(pattern)
        if idx != -1:
            # Grab text after the title up to next newline or punctuation
            after = text[idx + len(pattern) : idx + len(pattern) + 60]
            # Try to extract a name (letters, spaces, dots, hyphens)
            name_match = re.match(r"[\s:：]*([A-Z][a-zA-Z\s\.\-]{1,25}?)(?:[,;\n|]|$)", after)
            if not name_match:
                # Broader match but validate it looks like a person name
                name_match = re.match(r"[\s:：]*([A-Z][a-z]+\s+[A-Z][a-z]+)", after)
            if name_match:
                name = name_match.group(1).strip()
                # Strip trailing stop-words (Address, Email, Phone, etc.)
                name_words = name.split()
                while name_words and name_words[-1].lower().rstrip(".,:") in _CONTACT_TRAILING_STOPWORDS:
                    name_words.pop()
                name = " ".join(name_words)
                # Validate: ≤3 words after the title, 2-30 chars, no nav words
                if (2 <= len(name) <= 30
                        and len(name_words) <= 3
                        and not any(w in name.lower() for w in ("home", "about", "product", "service", "blog"))):
                    return re.sub(r"\s+", " ", f"{pattern} {name}").strip()
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
    for name in _extract_legal_names(soup):
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
            for name in _extract_legal_names(soup):
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
    """Try to extract factory/city/province location mentions.

    Prefers the "City, Province, China" pattern.  For other city/province
    matches, only accepts if the place name is in our Chinese location
    whitelist.  Rejects fake "City" matches like "Machine City" using a
    stopword list.
    """
    locations: list[str] = []
    seen: set[str] = set()

    # Sort by length descending so "Inner Mongolia" is tried before "Inner"
    sorted_locs = sorted(_CHINESE_LOCATIONS, key=len, reverse=True)

    # --- Preferred: "City, Province, China" pattern ---
    china_pattern = re.compile(
        r"(" + "|".join(re.escape(c) for c in sorted_locs)
        + r")\s*(?:City)?\s*,\s*("
        + "|".join(re.escape(p) for p in sorted_locs)
        + r")\s*(?:Province)?\s*,\s*China\b"
    )
    for m in china_pattern.finditer(text):
        city = m.group(1).strip()
        prov = m.group(2).strip()
        loc = f"{city}, {prov}, China"
        if loc not in seen:
            seen.add(loc)
            locations.append(loc)

    # --- Secondary: "City, Province" pattern ---
    cp_pattern = re.compile(
        r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s*(?:City)?\s*,\s*"
        r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s*(?:Province|City)"
    )
    for m in cp_pattern.finditer(text):
        city_part = m.group(1).strip()
        prov_part = m.group(2).strip()
        # Reject stopwords
        if city_part.lower() in _LOCATION_CITY_STOPWORDS:
            continue
        # The city or province must be in our whitelist
        if city_part in _CHINESE_LOCATIONS or prov_part in _CHINESE_LOCATIONS:
            loc = m.group(0).strip()
            if loc not in seen:
                seen.add(loc)
                locations.append(loc)

    # --- Tertiary: direct whitelist match with suffix ---
    for loc_name in sorted_locs:
        # Match "[loc_name] City" or "[loc_name] Province" etc.
        pattern = re.compile(
            re.escape(loc_name) + r"\s+(Province|City|District|County|Town|Village|"
            r"Industrial\s*(?:Zone|Area|Park)|Autonomous\s+Region)",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            loc = m.group(0).strip()
            if loc not in seen:
                seen.add(loc)
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
            for name in _extract_legal_names(soup):
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
            for name in _extract_legal_names(soup):
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
      Has legal_name:           +15
      Has email:                +10
      Has contact_name:         +10
      Has wechat_id:            +5
      Certs (count):  0→0, 1-2→10, 3-4→15, 5+→20
      Has factory_locations:    +10
      Has moq:                  +10
      Has brands_worked_with:   +10
      supplier_type:  Manufacturer→10, Trading Company→5
      Founded >10 yrs ago:       +5
      Platform verified:         +5
      Cap at 100
    """
    score = 0

    # Legal name
    if sr.legal_name:
        score += 15

    # Email
    if sr.email:
        score += 10

    # Contact name
    if sr.contact_name:
        score += 10

    # WeChat
    if sr.wechat_id:
        score += 5

    # Certifications
    cert_count = len(sr._certs)
    if cert_count >= 5:
        score += 20
    elif cert_count >= 3:
        score += 15
    elif cert_count >= 1:
        score += 10

    # Factory locations
    if sr.factory_locations:
        score += 10

    # MOQ
    if sr.moq:
        score += 10

    # Brands worked with
    if sr.brands_worked_with:
        score += 10

    # Supplier type
    if sr.supplier_type:
        stype_lower = sr.supplier_type.lower()
        if "manufacturer" in stype_lower:
            score += 10
        elif "trading" in stype_lower:
            score += 5

    # Founded >10 years ago
    if sr.founding_year:
        try:
            founded = int(sr.founding_year)
            current_year = 2026
            if current_year - founded > 10:
                score += 5
        except ValueError:
            pass
    if sr.platform_years and sr.platform_years > 10:
        score += 5

    # Platform verified
    if sr.platform_verified:
        score += 5

    return min(score, 100)


def _calc_completeness_score(sr: ScrapeResult) -> int:
    """
    Calculate data completeness score 0-100.

    Count non-null/non-empty fields out of 14 key fields:
      trade_name, legal_name, website, email, contact_name,
      wechat_id, factory_locations, certs_and_audits, moq,
      brands_worked_with, supplier_type, product_categories,
      market_experience, qualification_score

    Return (filled / 14) * 100, rounded.
    """
    # We use sr fields plus the input supplier dict's trade_name / website
    # Since we don't have the original dict here, we check sr + known fields
    filled = 0
    total = 14

    fields = [
        sr.legal_name,
        sr.email,
        sr.contact_name,
        sr.wechat_id,
        sr.factory_locations,
        sr.certs_and_audits,
        sr.moq,
        sr.brands_worked_with,
        sr.supplier_type,
        sr.product_categories,
        sr.market_experience,
        sr.founding_year,  # proxy for market_experience detail
        sr.phone,
        sr.company_description,
    ]

    for fval in fields:
        if fval is not None and str(fval).strip():
            filled += 1

    return int(round(100 * filled / total))


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
