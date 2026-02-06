import os
import re
import json
import smtplib
import ssl
import time
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from dateutil import tz
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import SOURCES, WEIGHTS, INTENSITY_KEYWORDS, SCARCITY_KEYWORDS

DEFAULT_RESY_IMAGE = (os.environ.get("DEFAULT_RESY_IMAGE") or "").strip() or None
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or None


@dataclass
class Restaurant:
    name: str
    url: Optional[str] = None          # primary “reserve / info” link
    image_url: Optional[str] = None    # thumbnail (final)
    neighborhood: Optional[str] = None
    cuisine: Optional[str] = None
    why_hot: Optional[str] = None
    sources: Optional[List[str]] = None
    heat_score: int = 0
    res_difficulty: str = "Moderate"
    booking_tip: str = "Book ahead when possible; aim for off-peak times."
    notes: Optional[str] = None


# ---------------------------
# Basic helpers
# ---------------------------

def _strip_leading_numbering(name: str) -> str:
    if not name:
        return name
    return re.sub(r"^\s*\d+\s*[\.\)\-–—:]\s*", "", name).strip()


def _leading_num(text: str) -> Optional[int]:
    m = re.match(r"^\s*(\d{1,3})\s*[\.\)\-–—:]\s*", text or "")
    return int(m.group(1)) if m else None


def _norm_name(name: str) -> str:
    n = _strip_leading_numbering(name or "")
    n = n.lower().strip()
    n = re.sub(r"[\u2019’]", "'", n)
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"[^a-z0-9 '&-]", "", n)
    return n


def _abs_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    return urljoin(base, href)


def fetch_html(url: str, timeout: int = 45, retries: int = 4) -> str:
    headers = {
        "User-Agent": "nyc-heat-index-bot/3.3 (+https://github.com/)",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "close",
    }

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=(15, timeout))
            r.raise_for_status()
            return r.text
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError) as e:
            last_err = e
            sleep_s = min(60, (2 ** (attempt - 1)) * 2) + random.random()
            print(f"Fetch failed ({attempt}/{retries}) for {url}: {e}. Retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)

    if last_err:
        raise last_err
    raise RuntimeError(f"Failed to fetch {url} for unknown reasons.")


# ---------------------------
# Resy heading cleanup (removes “map” UI text)
# ---------------------------

def _resy_clean_heading_text(h: Tag) -> str:
    """
    Resy sometimes includes inline UI like “Hudson Square map” in the same heading.
    Remove obvious “map” UI nodes, then read the heading text.
    Robust against rare bs4 elements with attrs=None.
    """
    try:
        h2 = BeautifulSoup(str(h), "lxml").find(h.name)
    except Exception:
        h2 = None

    if not h2:
        text = h.get_text(" ", strip=True)
        text = re.sub(r"\s+map\s*$", "", text, flags=re.I).strip()
        return text

    for t in h2.find_all(["span", "a", "small"]):
        if not isinstance(t, Tag):
            continue

        attrs = t.attrs or {}
        txt = t.get_text(" ", strip=True)
        txt_low = (txt or "").lower()
        aria_low = (attrs.get("aria-label") or "").lower()
        cls_low = " ".join(attrs.get("class", [])).lower() if attrs.get("class") else ""

        if txt_low in ("map", "view map") or txt_low.endswith(" map") or "map" in aria_low:
            t.decompose()
            continue
        if "map" in cls_low and len(txt_low) <= 20:
            t.decompose()
            continue

    text = h2.get_text(" ", strip=True)
    text = re.sub(r"\s+map\s*$", "", text, flags=re.I).strip()
    text = re.sub(r"\s+[A-Za-z][A-Za-z '&.-]{1,40}\s+map\s*$", "", text, flags=re.I).strip()
    return text


# ---------------------------
# Parsing helpers: slices and links
# ---------------------------

def _collect_between(start: Tag, end: Optional[Tag]) -> List[Tag]:
    nodes: List[Tag] = []
    for el in start.next_elements:
        if el is end:
            break
        if isinstance(el, Tag):
            nodes.append(el)
    return nodes


def _entry_slice_nodes_from_list(headings: List[Tag], idx: int) -> List[Tag]:
    end = headings[idx + 1] if idx + 1 < len(headings) else None
    return _collect_between(headings[idx], end)


def _first_real_paragraph_from_slice(nodes: List[Tag]) -> Optional[str]:
    """
    Skip UI cruft like “View in list”, “View in map”, etc.
    Prefer a real descriptive paragraph (length threshold).
    """
    bad_exact = {"view in list", "view in map", "map", "read more"}
    for node in nodes:
        if not isinstance(node, Tag):
            continue

        ps: List[Tag] = []
        if node.name == "p":
            ps.append(node)
        ps.extend(node.find_all("p"))

        for p in ps:
            txt = p.get_text(" ", strip=True)
            if not txt:
                continue
            low = txt.lower().strip()
            if low in bad_exact:
                continue
            if low.startswith(("view in ", "see more", "discover more")):
                continue
            if len(txt) < 40:
                continue
            return txt

    return None


def _is_useful_outbound(href: str, base_domain: str) -> bool:
    try:
        u = urlparse(href)
        if not u.scheme.startswith("http"):
            return False
        if u.netloc.endswith(base_domain):
            return False
        return True
    except Exception:
        return False


def _pick_link_from_slice(nodes: List[Tag], base_url: str, base_domain: str) -> Optional[str]:
    hrefs: List[str] = []
    for node in nodes:
        for a in node.find_all("a", href=True):
            u = _abs_url(base_url, a["href"])
            if u and u.startswith("http"):
                hrefs.append(u)

    for u in hrefs:
        if "resy.com" in u or "opentable.com" in u:
            return u

    for u in hrefs:
        if _is_useful_outbound(u, base_domain):
            return u

    return hrefs[0] if hrefs else None


# ---------------------------
# Image helpers (img/srcset/picture)
# ---------------------------

def _img_src_from_tag(img) -> Optional[str]:
    for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-url"):
        v = img.get(attr)
        if v:
            return v.strip()

    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        candidates: List[str] = []
        for part in srcset.split(","):
            url = part.strip().split(" ")[0].strip()
            if url:
                candidates.append(url)
        if candidates:
            return candidates[-1]
    return None


def _src_from_source_tag(source) -> Optional[str]:
    srcset = source.get("srcset") or source.get("data-srcset")
    if not srcset:
        return None
    candidates: List[str] = []
    for part in srcset.split(","):
        url = part.strip().split(" ")[0].strip()
        if url:
            candidates.append(url)
    return candidates[-1] if candidates else None


# ---------------------------
# Eater image picking (stop at modules like “See more”)
# ---------------------------

_EATER_STOP_WORDS = {"see more", "related", "more maps", "more maps in eater ny", "you might also like"}


def _hit_stop_boundary(node: Tag) -> bool:
    if node.name in ("aside", "footer", "nav"):
        return True
    txt = node.get_text(" ", strip=True).lower() if isinstance(node, Tag) else ""
    return any(w in txt for w in _EATER_STOP_WORDS)


def _pick_image_from_slice(
    nodes: List[Tag],
    base_url: str,
    restaurant_name: str,
    *,
    max_tags_to_scan: int = 60,
    stop_on_modules: bool = True,
) -> Optional[str]:
    name_norm = _norm_name(restaurant_name)
    candidates: List[Tuple[int, str]] = []

    def score_url(u: str, alt: str) -> int:
        u_low = u.lower()
        alt_norm = _norm_name(alt or "")
        score = 0
        if name_norm and name_norm in alt_norm:
            score += 50
        if any(bad in u_low for bad in ["logo", "icon", "avatar", "spinner", "placeholder", "sprite"]):
            score -= 40
        if u_low.endswith(".svg"):
            score -= 30
        return score

    scanned = 0
    for node in nodes:
        if not isinstance(node, Tag):
            continue
        scanned += 1
        if scanned > max_tags_to_scan:
            break
        if stop_on_modules and _hit_stop_boundary(node):
            break

        pic = node.find("picture")
        if pic:
            src = pic.find("source")
            if src:
                u = _src_from_source_tag(src)
                if u:
                    u2 = _abs_url(base_url, u)
                    if u2:
                        candidates.append((score_url(u2, ""), u2))

        for img in node.find_all("img"):
            u = _img_src_from_tag(img)
            if not u:
                continue
            u2 = _abs_url(base_url, u)
            if not u2:
                continue
            alt = img.get("alt", "") or ""
            candidates.append((score_url(u2, alt), u2))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ---------------------------
# OG image fallback (restricted; do NOT use for Resy-only cards)
# ---------------------------

def og_image(url: str) -> Optional[str]:
    try:
        html = fetch_html(url, timeout=30, retries=2)
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
        if tag and tag.get("content"):
            return tag["content"].strip()
    except Exception:
        return None
    return None


def _ok_for_og(url: str) -> bool:
    u = (url or "").lower()
    # Allow Eater venue pages only
    if "ny.eater.com/venue/" in u:
        return True

    # Block booking redirectors/aggregators (often generic OG images)
    if "reservations.safegraph.com" in u:
        return False
    if "exploretock.com" in u:
        return False

    if "eater.com" in u:
        return False
    if "blog.resy.com" in u:
        return False
    return True


# ---------------------------
# Name heuristics
# ---------------------------

_BAD_NAME_FRAGMENTS = [
    "newest restaurant openings", "now on resy", "newsletter", "sign up", "subscribe",
    "follow us", "gift card", "resy events", "private dining", "more from resy",
    "read more", "updated", "where to eat", "the hit list", "the heatmap",
]

_REJECT_EXACT = {
    "about", "careers", "nearby restaurants", "top rated", "new on resy", "events",
    "features", "plans & pricing", "why resy os", "request a demo", "resy help desk",
    "global privacy policy", "terms of service", "cookie policy", "accessibility statement",
    "resy os overview", "resy os dashboard", "for restaurants", "resy", "get resy emails",
    "the resy credit", "global dining access", "discover more", "craving something else",
}


def _looks_like_restaurant_name(name: str) -> bool:
    if not name:
        return False
    n = _strip_leading_numbering(name.strip())
    if len(n) < 2 or len(n) > 80:
        return False

    low = n.lower()
    if low in _REJECT_EXACT:
        return False

    if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}$", low):
        return False

    if low.startswith(("a ", "an ", "the ", "watch ", "viewing ", "buffet ", "four-course")):
        return False

    if any(bad in low for bad in _BAD_NAME_FRAGMENTS):
        return False

    if re.fullmatch(r"[0-9\s\-/,:.]+", n):
        return False

    return True


# ---------------------------
# Resy URL gating + strict image picking (Hit List ONLY)
# ---------------------------

def _is_resy_restaurant_booking_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()

    if "opentable.com" in u:
        return True
    if "resy.com" not in u:
        return False

    reject_fragments = [
        "resy-os", "/os", "help", "privacy", "terms", "cookie", "accessibility",
        "/about", "/careers", "/press", "/for-restaurants", "/request-a-demo",
        "pricing", "features", "dashboard", "overview", "/gift", "/gifts", "/credit",
    ]
    if any(frag in u for frag in reject_fragments):
        return False

    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False

    if "/r/" in path:
        return True
    if "/venues/" in path or "/places/" in path or "/restaurants/" in path:
        return True

    if path.startswith("/cities/"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3:
            slug = parts[2]
            bad_slugs = {
                "search", "nearby", "neighborhoods", "guides", "collections",
                "events", "top-rated", "top", "new", "new-on-resy", "restaurants",
                "dining", "about",
            }
            if slug in bad_slugs:
                return False
            if len(parts) >= 4 and parts[2] in bad_slugs:
                return False
            return True

    return False


def _is_legit_resy_hitlist_photo(u: str) -> bool:
    # Only accept photos that appear on the Hit List page and come from Resy's image CDN.
    u_low = (u or "").lower().strip()
    return u_low.startswith("https://image.resy.com/")


_RESY_IMAGE_STOP_PHRASES = ("discover more", "craving something else", "craving something else?")


def _pick_resy_image_for_heading(h: Tag, base_url: str, restaurant_name: str) -> Optional[str]:
    """
    Hit List ONLY: scan forward from the heading and accept only image.resy.com photos.
    Do not look up venue pages; if no photo in the entry itself, return None.
    """
    name_norm = _norm_name(restaurant_name)
    best_score = -10_000
    best_url: Optional[str] = None

    def score(u: str, alt: str) -> int:
        u_low = (u or "").lower()
        alt_norm = _norm_name(alt or "")
        s = 0

        hard_reject = [
            "s3.amazonaws.com/resy.com/images/social/",
            "facebook-preview",
            "/social/",
            "/icons/",
            "/icon/",
            "/logo",
            "placeholder",
            "sprite",
            "spinner",
        ]
        if any(x in u_low for x in hard_reject):
            return -10_000

        if not _is_legit_resy_hitlist_photo(u_low):
            return -10_000

        if name_norm and name_norm in alt_norm:
            s += 80
        else:
            s -= 20

        return s

    steps = 0
    for el in h.next_elements:
        if not isinstance(el, Tag):
            continue
        steps += 1
        if steps > 220:
            break

        if el.name in ("h2", "h3", "h4"):
            t = el.get_text(" ", strip=True)
            if _leading_num(t) is not None:
                break
            low = t.lower()
            if any(p in low for p in _RESY_IMAGE_STOP_PHRASES):
                break

        pic = el.find("picture")
        if pic:
            src = pic.find("source")
            if src:
                u = _src_from_source_tag(src)
                if u:
                    u2 = _abs_url(base_url, u)
                    if u2 and _is_legit_resy_hitlist_photo(u2):
                        sc = score(u2, "")
                        if sc > best_score:
                            best_score, best_url = sc, u2

        for img in el.find_all("img"):
            u = _img_src_from_tag(img)
            if not u:
                continue
            u2 = _abs_url(base_url, u)
            if not u2 or not _is_legit_resy_hitlist_photo(u2):
                continue
            sc = score(u2, img.get("alt", "") or "")
            if sc > best_score:
                best_score, best_url = sc, u2

    return best_url if best_score >= 60 else None


# ---------------------------
# Cache/Re-host Resy images for GitHub Pages reliability
# ---------------------------

def _slugify(s: str) -> str:
    s = _norm_name(s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:80] or "img"


def _cache_remote_image(url: str, out_path: str) -> bool:
    """
    Download an image to the dist/assets directory so it renders on GitHub Pages.
    Uses a Referer header to reduce chance of hotlink blocks.
    """
    try:
        headers = {
            "User-Agent": "nyc-heat-index-bot/3.3 (+https://github.com/)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://blog.resy.com/",
        }
        r = requests.get(url, headers=headers, timeout=(15, 45))
        if r.status_code != 200 or not r.content:
            return False

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception:
        return False


def cache_resy_images_for_pages(restaurants: List[Restaurant], dist_dir: str) -> None:
    """
    Re-host Resy Hit List images under dist/assets so GitHub Pages can load them.
    Only caches images already found on the Hit List page (image.resy.com).
    If caching fails, fall back to DEFAULT_RESY_IMAGE.
    """
    assets_dir = os.path.join(dist_dir, "assets")
    cached = 0
    attempted = 0

    for r in restaurants:
        is_resy_only = r.sources and ("Resy" in r.sources) and ("Eater" not in r.sources)
        if not is_resy_only or not r.image_url:
            continue

        if not r.image_url.lower().startswith("https://image.resy.com/"):
            continue

        attempted += 1
        fname = f"resy-{_slugify(r.name)}.jpg"
        out_path = os.path.join(assets_dir, fname)

        ok = _cache_remote_image(r.image_url, out_path)
        if ok:
            cached += 1
            if PUBLIC_BASE_URL:
                r.image_url = f"{PUBLIC_BASE_URL}/assets/{fname}"
            else:
                r.image_url = f"assets/{fname}"
        else:
            if DEFAULT_RESY_IMAGE:
                r.image_url = DEFAULT_RESY_IMAGE
            else:
                r.image_url = None

    print(f"[ResyCache] attempted={attempted} cached={cached} public_base_set={bool(PUBLIC_BASE_URL)}")


# ---------------------------
# Source extractors
# ---------------------------

def extract_resy_hit_list(html: str) -> List[Restaurant]:
    base_url = "https://blog.resy.com"
    base_domain = "resy.com"

    soup = BeautifulSoup(html, "lxml")

    def score_scope(tag: Optional[Tag]) -> int:
        if not tag or not isinstance(tag, Tag):
            return -1
        p = len(tag.find_all("p"))
        h = len(tag.find_all(["h2", "h3", "h4"]))
        b = len(tag.find_all(["strong", "b"]))
        txt_len = len(tag.get_text(" ", strip=True))
        return (p * 6) + (h * 10) + (b * 3) + (txt_len // 1000)

    candidates: List[Tag] = []
    candidates.extend(soup.find_all("article"))
    candidates.extend(soup.find_all("main"))
    candidates.extend([
        t for t in soup.select(
            ".entry-content, .post-content, .article-content, .content, "
            ".single-content, .post__content, [data-testid='post-content']"
        ) if isinstance(t, Tag)
    ])
    if soup.body:
        candidates.append(soup.body)

    scope = max(candidates, key=score_scope) if candidates else (soup.body or soup)

    def in_nav(tag: Tag) -> bool:
        for p in tag.parents:
            if getattr(p, "name", None) in ("header", "footer", "nav", "aside"):
                return True
            cls = " ".join(p.get("class", [])).lower() if isinstance(p, Tag) else ""
            if any(x in cls for x in ["footer", "nav", "menu", "header", "cookie", "consent"]):
                return True
        return False

    RESY_STOP_PHRASES = (
        "discover more", "recommended", "more from resy", "related",
        "you might also like", "craving something else", "craving something else?",
    )

    def is_stop_heading(h: Tag) -> bool:
        txt = h.get_text(" ", strip=True).lower()
        return any(p in txt for p in RESY_STOP_PHRASES)

    def is_numbered_restaurant_heading(h: Tag) -> bool:
        raw = _resy_clean_heading_text(h)
        n = _leading_num(raw)
        if n is None:
            return False
        name = _strip_leading_numbering(raw)
        return _looks_like_restaurant_name(name)

    headings_all = [h for h in scope.find_all(["h2", "h3", "h4"]) if not in_nav(h)]

    stop_idx: Optional[int] = None
    for idx, h in enumerate(headings_all):
        if is_stop_heading(h):
            stop_idx = idx
            break

    last_num_idx: Optional[int] = None
    last_num_value: Optional[int] = None
    for idx, h in enumerate(headings_all):
        if is_numbered_restaurant_heading(h):
            n = _leading_num(_resy_clean_heading_text(h))
            if n is not None and (last_num_value is None or n >= last_num_value):
                last_num_value = n
                last_num_idx = idx

    if last_num_idx is not None and stop_idx is not None and last_num_idx < stop_idx:
        end_exclusive = last_num_idx + 1
    elif stop_idx is not None:
        end_exclusive = stop_idx
    elif last_num_idx is not None:
        end_exclusive = last_num_idx + 1
    else:
        end_exclusive = len(headings_all)

    headings = headings_all[:end_exclusive]

    restaurants: List[Restaurant] = []

    for i, h in enumerate(headings):
        raw = _resy_clean_heading_text(h)
        if _leading_num(raw) is None:
            continue  # numbered entries only

        name = _strip_leading_numbering(raw)
        if not _looks_like_restaurant_name(name):
            continue

        nodes = _entry_slice_nodes_from_list(headings, i)
        why = _first_real_paragraph_from_slice(nodes)
        image_url = _pick_resy_image_for_heading(h, base_url=base_url, restaurant_name=name)

        url = _pick_link_from_slice(nodes, base_url=base_url, base_domain=base_domain)
        if url and not _is_resy_restaurant_booking_url(url):
            url = None

        restaurants.append(Restaurant(
            name=name,
            url=url,
            image_url=image_url,
            why_hot=why,
            sources=["Resy"],
        ))

    cleaned: List[Restaurant] = []
    for r in restaurants:
        r.name = _strip_leading_numbering(r.name)
        if not _looks_like_restaurant_name(r.name):
            continue
        if r.name.lower() in _REJECT_EXACT:
            continue
        cleaned.append(r)

    print(
        f"[Resy] scope_tag={getattr(scope,'name',None)} score={score_scope(scope)} "
        f"headings_all={len(headings_all)} stop_idx={stop_idx} "
        f"last_num={last_num_value} last_num_idx={last_num_idx} "
        f"headings_used={len(headings)} extracted={len(cleaned)} html_len={len(html)}"
    )
    return cleaned


def extract_eater_heatmap(html: str) -> List[Restaurant]:
    base_url = "https://ny.eater.com"
    base_domain = "eater.com"

    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find("main") or soup

    restaurants: List[Restaurant] = []
    headings = article.find_all(["h2", "h3"])

    for i, h in enumerate(headings):
        title = _strip_leading_numbering(h.get_text(" ", strip=True))
        if not _looks_like_restaurant_name(title):
            continue

        low = title.lower()
        if any(k in low for k in ["related", "updates"]):
            continue
        if low in ("see more", "more maps in eater ny") or low.startswith("more maps"):
            continue

        name = title
        nodes = _entry_slice_nodes_from_list(headings, i)

        why = _first_real_paragraph_from_slice(nodes)
        image_url = _pick_image_from_slice(
            nodes,
            base_url=base_url,
            restaurant_name=name,
            max_tags_to_scan=40,
            stop_on_modules=True,
        )

        # Prefer Eater venue links if present
        venue = None
        for node in nodes:
            if not isinstance(node, Tag):
                continue
            for a in node.find_all("a", href=True):
                u = _abs_url(base_url, a["href"])
                if u and "ny.eater.com/venue/" in u:
                    venue = u
                    break
            if venue:
                break

        url = venue or _pick_link_from_slice(nodes, base_url=base_url, base_domain=base_domain)

        if not url:
            a = h.find("a", href=True)
            url = _abs_url(base_url, a["href"]) if a else None

        restaurants.append(Restaurant(
            name=name,
            url=url,
            image_url=image_url,
            why_hot=why,
            sources=["Eater"],
        ))

    return restaurants


# ---------------------------
# Merge / scoring / output
# ---------------------------

def dedupe(items: List[Restaurant]) -> List[Restaurant]:
    seen: Dict[str, Restaurant] = {}

    for r in items:
        r.name = _strip_leading_numbering(r.name)
        key = _norm_name(r.name)
        if key not in seen:
            seen[key] = r
            continue

        e = seen[key]
        e.sources = sorted(list(set((e.sources or []) + (r.sources or []))))
        e.url = e.url or r.url
        e.why_hot = e.why_hot or r.why_hot
        e.neighborhood = e.neighborhood or r.neighborhood
        e.cuisine = e.cuisine or r.cuisine

        eater_img = None
        if (e.sources and "Eater" in (e.sources or []) and e.image_url):
            eater_img = e.image_url
        if (r.sources and "Eater" in (r.sources or []) and r.image_url):
            eater_img = r.image_url

        if eater_img:
            e.image_url = eater_img
        else:
            e.image_url = e.image_url or r.image_url

    return list(seen.values())


def load_state(path: str) -> Dict:
    if not os.path.exists(path):
        return {"last_month_names": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, names: List[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_month_names": names}, f, indent=2)


def keyword_score(text: str, buckets: Dict[int, List[str]], max_score: int) -> int:
    if not text:
        return 0
    t = text.lower()
    best = 0
    for score, kws in buckets.items():
        for kw in kws:
            if kw in t:
                best = max(best, score)
    return min(best, max_score)


def reservation_intel(r: Restaurant) -> Tuple[str, str]:
    text = (r.why_hot or "").lower()

    platform = None
    if r.url and "resy.com" in r.url:
        platform = "Resy"
    elif r.url and "opentable.com" in r.url:
        platform = "OpenTable"

    brutal = any(k in text for k in ["impossible", "sold out", "months out", "hardest", "booked up"])
    hard = any(k in text for k in ["hard to book", "tough reservation", "set your alarm", "reservation release", "drops"])
    easy = any(k in text for k in ["walk-in friendly", "plenty of seats", "easy to book", "no problem getting in"])

    if brutal:
        diff = "Brutal"
    elif hard:
        diff = "Hard"
    elif easy:
        diff = "Easy"
    else:
        diff = "Moderate"

    if "walk-in" in text or "walk ins" in text:
        tip = "Treat it like a walk-in: go early (before 6pm) or late, and be flexible on party size."
    elif platform == "Resy" or "resy" in text:
        tip = "Use Resy: check common drop times (often mornings), enable notifications, and target bar/counter seats."
    elif platform == "OpenTable" or "opentable" in text:
        tip = "Use OpenTable: book 1–2 weeks out, then set alerts for earlier times and watch for last-minute openings."
    else:
        tip = "Book ahead where possible; otherwise aim for off-peak (early/late) and consider bar seating."

    return diff, tip


def compute_heat(restaurants: List[Restaurant], last_month_names: List[str]) -> None:
    last_set = set(_norm_name(n) for n in last_month_names)

    for r in restaurants:
        srcs = set((r.sources or []))
        score = 0

        if "Resy" in srcs and "Eater" in srcs:
            score += WEIGHTS["both_sources_bonus"]

        if _norm_name(r.name) in last_set:
            score += WEIGHTS["carried_over_bonus"]
        else:
            score += WEIGHTS["new_this_month_bonus"]

        why = r.why_hot or ""
        score += keyword_score(why, INTENSITY_KEYWORDS, WEIGHTS["language_intensity_max"])
        score += keyword_score(why, SCARCITY_KEYWORDS, WEIGHTS["reservation_scarcity_max"])

        r.heat_score = max(0, min(100, int(score)))
        r.res_difficulty, r.booking_tip = reservation_intel(r)

    restaurants.sort(key=lambda x: x.heat_score, reverse=True)


def render_newsletter(output_dir: str, restaurants: List[Restaurant]) -> Tuple[str, str]:
    tz_name = os.environ.get("TIMEZONE") or "America/New_York"
    local_tz = tz.gettz(tz_name)

    now_local = datetime.now(timezone.utc).astimezone(local_tz)
    month_label = now_local.strftime("%B %Y")
    title = f"NYC Heat Index — {month_label}"

    env = Environment(
        loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "..", "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tpl = env.get_template("newsletter.html.j2")

    html = tpl.render(
        title=title,
        period_label=month_label,
        generated_at=now_local.strftime("%Y-%m-%d %H:%M %Z"),
        restaurants=[asdict(r) for r in restaurants],
        sources=[{"name": s.name, "url": s.url} for s in SOURCES],
    )

    issue_slug = now_local.strftime("%Y-%m-%d")
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"issue-{issue_slug}.html")
    index_path = os.path.join(output_dir, "index.html")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    return title, out_path


def send_email(subject: str, html_body: str) -> None:
    dry_run = (os.environ.get("DRY_RUN") or "").lower() in ("1", "true", "yes")
    if dry_run:
        print("DRY_RUN enabled: skipping SMTP send.")
        return

    subscribers = [e.strip() for e in (os.environ.get("SUBSCRIBERS") or "").split(",") if e.strip()]
    if not subscribers:
        raise RuntimeError("SUBSCRIBERS secret is empty. Provide comma-separated emails.")

    host = (os.environ.get("SMTP_HOST") or "").strip()
    username = (os.environ.get("SMTP_USERNAME") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    if not host or not username or not password:
        raise RuntimeError("SMTP secrets missing/blank. Set SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD (and optionally SMTP_PORT).")

    port_raw = (os.environ.get("SMTP_PORT") or "").strip()
    port = int(port_raw) if port_raw else 587

    from_email = (os.environ.get("FROM_EMAIL") or username).strip()
    from_name = (os.environ.get("FROM_NAME") or "NYC Heat Index").strip()
    reply_to = (os.environ.get("REPLY_TO") or "").strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = from_email
    msg["Bcc"] = ", ".join(subscribers)
    if reply_to:
        msg["Reply-To"] = reply_to

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.sendmail(from_email, [from_email] + subscribers, msg.as_string())


def main() -> None:
    state_path = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")
    state = load_state(state_path)
    last_month_names = state.get("last_month_names", [])

    all_items: List[Restaurant] = []

    for s in SOURCES:
        print(f"Fetching: {s.name} -> {s.url}")
        html = fetch_html(s.url)

        if "resy.com" in s.url or "blog.resy.com" in s.url:
            items = extract_resy_hit_list(html)
        elif "eater.com" in s.url:
            items = extract_eater_heatmap(html)
        else:
            items = []

        print(f"{s.name}: extracted {len(items)} items")
        all_items.extend(items)

    merged = dedupe(all_items)
    for r in merged:
        r.sources = sorted(list(set(r.sources or [])))

    # OG fill (capped) — NEVER for Resy-only items
    og_filled = 0
    for r in merged:
        if og_filled >= 10:
            break

        is_resy_only = r.sources and ("Resy" in r.sources) and ("Eater" not in r.sources)
        if is_resy_only:
            continue

        if (not r.image_url) and r.url and _ok_for_og(r.url):
            img = og_image(r.url)
            if img:
                r.image_url = img
                og_filled += 1

    # Default Resy image for Resy-only items missing a Hit List photo
    if DEFAULT_RESY_IMAGE:
        for r in merged:
            if r.sources and ("Resy" in r.sources) and ("Eater" not in r.sources):
                if not r.image_url:
                    r.image_url = DEFAULT_RESY_IMAGE

    compute_heat(merged, last_month_names)

    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")

    # Cache/re-host Resy images so GitHub Pages doesn't show broken hotlinked images
    cache_resy_images_for_pages(merged, dist_dir=dist_dir)

    title, out_path = render_newsletter(output_dir=dist_dir, restaurants=merged)
    with open(out_path, "r", encoding="utf-8") as f:
        html_body = f.read()

    save_state(state_path, [r.name for r in merged])
    send_email(subject=title, html_body=html_body)

    print("Generated:", out_path)
    print("Title:", title)
    print("Total cards:", len(merged))
    print("Images:", sum(1 for r in merged if r.image_url), "of", len(merged))
    print("[Config] DEFAULT_RESY_IMAGE set:", bool(DEFAULT_RESY_IMAGE))
    print("[Config] PUBLIC_BASE_URL set:", bool(PUBLIC_BASE_URL))

if __name__ == "__main__":
    main()
