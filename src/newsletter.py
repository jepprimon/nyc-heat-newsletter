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


@dataclass
class Restaurant:
    name: str
    url: Optional[str] = None          # primary “reserve / info” link
    image_url: Optional[str] = None    # thumbnail
    neighborhood: Optional[str] = None
    cuisine: Optional[str] = None
    why_hot: Optional[str] = None
    sources: Optional[List[str]] = None
    heat_score: int = 0
    res_difficulty: str = "Moderate"
    booking_tip: str = "Book ahead when possible; aim for off-peak times."
    notes: Optional[str] = None


# ---------------------------
# Utility helpers
# ---------------------------

def _strip_leading_numbering(name: str) -> str:
    if not name:
        return name
    # handles: "18. Foo", "18) Foo", "18 - Foo", "18 — Foo", "18: Foo"
    return re.sub(r"^\s*\d+\s*[\.\)\-–—:]\s*", "", name).strip()


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
        "User-Agent": "nyc-heat-index-bot/2.0 (+https://github.com/)",
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


# ---------------------------
# Robust entry slicing: between headings in document order
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


def _first_paragraph_from_slice(nodes: List[Tag]) -> Optional[str]:
    for node in nodes:
        if node.name == "p":
            txt = node.get_text(" ", strip=True)
            if txt:
                return txt
        p = node.find("p")
        if p:
            txt = p.get_text(" ", strip=True)
            if txt:
                return txt
    return None


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
# Image picking: proximity + module-boundary guards
# ---------------------------

_EATER_STOP_WORDS = {
    "see more",
    "related",
    "more maps",
    "more maps in eater ny",
    "you might also like",
}


def _hit_stop_boundary(node: Tag) -> bool:
    if node.name in ("aside", "footer", "nav"):
        return True
    txt = node.get_text(" ", strip=True).lower() if isinstance(node, Tag) else ""
    if txt:
        for w in _EATER_STOP_WORDS:
            if w in txt:
                return True
    return False


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
# OG image fallback (restricted)
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
    if "eater.com" in u:
        return False
    if "blog.resy.com" in u:
        return False
    return True


# ---------------------------
# Name heuristics + Resy-specific URL gating
# ---------------------------

_BAD_NAME_FRAGMENTS = [
    "newest restaurant openings",
    "now on resy",
    "newsletter",
    "sign up",
    "subscribe",
    "follow us",
    "gift card",
    "resy events",
    "private dining",
    "more from resy",
    "read more",
    "updated",
    "where to eat",
    "the hit list",
    "the heatmap",
]

# hard nav/footer junk that should never become a card
_REJECT_EXACT = {
    "about", "careers", "nearby restaurants", "climbing", "top rated", "new on resy",
    "events", "features", "plans & pricing", "why resy os", "request a demo",
    "resy help desk", "global privacy policy", "terms of service", "cookie policy",
    "accessibility statement", "resy os overview", "resy os dashboard",
    "for restaurants", "resy", "get resy emails", "the resy credit", "global dining access",
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


def _is_title_case(name: str) -> bool:
    words = [w for w in _strip_leading_numbering(name).split() if w]
    if not words:
        return False
    if len(words) == 1:
        return words[0][:1].isupper()
    lowers = {"of", "and", "the", "la", "le", "el", "de", "da", "di", "del"}
    ok = 0
    for w in words:
        if w.lower() in lowers:
            ok += 1
        elif w[:1].isupper():
            ok += 1
    return ok >= len(words) - 1


def _is_resy_restaurant_booking_url(url: str) -> bool:
    """
    Accept Resy/OpenTable URLs that plausibly point to a restaurant booking/venue page.
    Resy venue URLs commonly appear as:
      - https://resy.com/cities/ny/<restaurant-slug>
      - https://resy.com/cities/ny/venues/<slug>
      - https://resy.com/cities/ny/places/<slug>
      - https://resy.com/r/<slug>
    Reject OS/legal/help/marketing/etc.
    """
    if not url:
        return False
    u = url.lower()

    # OpenTable is fine
    if "opentable.com" in u:
        return True

    if "resy.com" not in u:
        return False

    # hard rejects (marketing / product / legal / help)
    reject_fragments = [
        "resy-os", "/os", "help", "privacy", "terms", "cookie", "accessibility",
        "/about", "/careers", "/press", "/for-restaurants", "/request-a-demo",
        "pricing", "features", "dashboard", "overview",
        "/gift", "/gifts", "/credit",
    ]
    if any(frag in u for frag in reject_fragments):
        return False

    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False

    # easy allow patterns
    if "/r/" in path:
        return True
    if "/venues/" in path or "/places/" in path or "/restaurants/" in path:
        return True

    # allow /cities/<city>/<slug> where slug is not a generic category page
    if path.startswith("/cities/"):
        parts = [p for p in path.split("/") if p]  # e.g. ['cities','ny','wild-cherry']
        if len(parts) >= 3:
            slug = parts[2]
            # reject non-restaurant city subpages
            bad_slugs = {
                "search", "nearby", "neighborhoods", "guides", "collections",
                "events", "top-rated", "top", "new", "new-on-resy", "restaurants",
                "dining", "about",
            }
            if slug in bad_slugs:
                return False
            # also reject if it's clearly a category path like /cities/ny/neighborhoods/...
            if len(parts) >= 4 and parts[2] in bad_slugs:
                return False
            return True

    return False


def _resy_name_from_context(a: Tag) -> Optional[str]:
    """
    Resy blog often has booking links with anchor text like 'Reserve' or 'Get Resy alerts'.
    This tries to infer the restaurant name from nearby context:
      - nearest previous strong/b within same paragraph/section
      - nearest previous h2/h3
      - aria-label/title attributes
    """
    # 1) aria-label / title sometimes contains name
    for attr in ("aria-label", "title"):
        v = a.get(attr)
        if v:
            cand = _strip_leading_numbering(v.strip())
            if _looks_like_restaurant_name(cand):
                return cand

    # 2) look within the same paragraph/container for strong/b text
    container = a.find_parent(["p", "li", "div", "section", "article"])
    if container:
        strong = container.find(["strong", "b"])
        if strong:
            cand = _strip_leading_numbering(strong.get_text(" ", strip=True))
            if _looks_like_restaurant_name(cand):
                return cand

    # 3) walk backwards through previous elements for a nearby name-like heading/strong
    steps = 0
    for el in a.previous_elements:
        if not isinstance(el, Tag):
            continue
        steps += 1
        if steps > 120:  # keep it local
            break
        if el.name in ("h2", "h3"):
            cand = _strip_leading_numbering(el.get_text(" ", strip=True))
            if _looks_like_restaurant_name(cand):
                return cand
        if el.name in ("strong", "b"):
            cand = _strip_leading_numbering(el.get_text(" ", strip=True))
            if _looks_like_restaurant_name(cand):
                return cand

    return None


def _fallback_from_outbound_links_resy(scope: Tag, base_url: str) -> List[Restaurant]:
    """
    Tight-but-correct Resy fallback:
      - only inside article scope (no footer/nav)
      - only accept links that look like restaurant/venue booking URLs
      - if anchor text isn't a restaurant name, infer it from context
    """
    out: List[Restaurant] = []
    seen: set[str] = set()

    for a in scope.find_all("a", href=True):
        href = _abs_url(base_url, a.get("href"))
        if not href or not href.startswith("http"):
            continue
        if not _is_resy_restaurant_booking_url(href):
            continue

        raw_text = _strip_leading_numbering(a.get_text(" ", strip=True))
        name = raw_text if _looks_like_restaurant_name(raw_text) else _resy_name_from_context(a)
        if not name:
            continue

        name = _strip_leading_numbering(name)
        if not _looks_like_restaurant_name(name):
            continue

        # Extra guard: avoid single generic words like "About", "Events"
        if name.lower() in _REJECT_EXACT:
            continue

        key = _norm_name(name)
        if key in seen:
            continue
        seen.add(key)

        out.append(Restaurant(name=name, url=href, sources=["Resy"]))

    return out


def _fallback_from_outbound_links_generic(scope: Tag, base_url: str, source_label: str) -> List[Restaurant]:
    soup = scope
    out: List[Restaurant] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = _abs_url(base_url, a.get("href"))
        if not href or not href.startswith("http"):
            continue
        hlow = href.lower()
        if not (("resy.com" in hlow) or ("opentable.com" in hlow)):
            continue

        text = _strip_leading_numbering(a.get_text(" ", strip=True))
        if not _looks_like_restaurant_name(text):
            continue

        key = _norm_name(text)
        if key in seen:
            continue
        seen.add(key)

        out.append(Restaurant(name=text, url=href, sources=[source_label]))

    return out


# ---------------------------
# Source extractors
# ---------------------------

def extract_resy_hit_list(html: str) -> List[Restaurant]:
    base_url = "https://blog.resy.com"
    base_domain = "resy.com"

    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup  # IMPORTANT: keep scope tight

    restaurants: List[Restaurant] = []

    headings = article.find_all(["h2", "h3"])

    # Heading-based extraction (Resy sometimes works)
    for i, h in enumerate(headings):
        raw = h.get_text(" ", strip=True)
        name = _strip_leading_numbering(raw)
        if not _looks_like_restaurant_name(name):
            continue

        nodes = _entry_slice_nodes_from_list(headings, i)
        why = _first_paragraph_from_slice(nodes)
        image_url = _pick_image_from_slice(nodes, base_url=base_url, restaurant_name=name, max_tags_to_scan=80, stop_on_modules=False)
        url = _pick_link_from_slice(nodes, base_url=base_url, base_domain=base_domain)

        # If url exists but is clearly not a venue booking url, drop it (prevents OS/legal bleed)
        if url and not _is_resy_restaurant_booking_url(url):
            url = None

        restaurants.append(
            Restaurant(
                name=name,
                url=url,
                image_url=image_url,
                why_hot=why,
                sources=["Resy"],
            )
        )

    restaurants = dedupe([r for r in restaurants if _looks_like_restaurant_name(r.name)])

    # Tight fallback: ONLY within article, ONLY venue-like booking URLs
fb = _fallback_from_outbound_links_resy(article, base_url=base_url)
restaurants = dedupe(restaurants + fb)

    # Final cleanup: remove any remaining exact rejects
    cleaned = []
    for r in restaurants:
        r.name = _strip_leading_numbering(r.name)
        if not _looks_like_restaurant_name(r.name):
            continue
        cleaned.append(r)

    return cleaned


def extract_eater_heatmap(html: str) -> List[Restaurant]:
    base_url = "https://ny.eater.com"
    base_domain = "eater.com"

    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup

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

        why = _first_paragraph_from_slice(nodes)
        image_url = _pick_image_from_slice(nodes, base_url=base_url, restaurant_name=name, max_tags_to_scan=40, stop_on_modules=True)
        url = _pick_link_from_slice(nodes, base_url=base_url, base_domain=base_domain)

        if not url:
            a = h.find("a", href=True)
            url = _abs_url(base_url, a["href"]) if a else None

        restaurants.append(
            Restaurant(
                name=name,
                url=url,
                image_url=image_url,
                why_hot=why,
                sources=["Eater"],
            )
        )

    restaurants = dedupe(restaurants)

    if len(restaurants) < 5:
        fb = _fallback_from_outbound_links_generic(article, base_url=base_url, source_label="Eater")
        restaurants = dedupe(restaurants + fb)

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
        else:
            e = seen[key]
            e.sources = sorted(list(set((e.sources or []) + (r.sources or []))))
            e.url = e.url or r.url
            e.image_url = e.image_url or r.image_url
            e.why_hot = e.why_hot or r.why_hot
            e.neighborhood = e.neighborhood or r.neighborhood
            e.cuisine = e.cuisine or r.cuisine
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

    for r in merged:
        if not r.image_url and r.url and _ok_for_og(r.url):
            r.image_url = og_image(r.url)

    compute_heat(merged, last_month_names)

    title, out_path = render_newsletter(
        output_dir=os.path.join(os.path.dirname(__file__), "..", "dist"),
        restaurants=merged,
    )
    with open(out_path, "r", encoding="utf-8") as f:
        html_body = f.read()

    save_state(state_path, [r.name for r in merged])
    send_email(subject=title, html_body=html_body)

    print("Generated:", out_path)
    print("Title:", title)
    print("Total cards:", len(merged))
    print("Images:", sum(1 for r in merged if r.image_url), "of", len(merged))


if __name__ == "__main__":
    main()
