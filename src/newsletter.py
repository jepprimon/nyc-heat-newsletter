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
    url: Optional[str] = None              # canonical details page (venue page or official site)
    reserve_url: Optional[str] = None      # booking link (Resy/OpenTable/Tock/SafeGraph) if present
    image_url: Optional[str] = None        # final thumbnail URL
    neighborhood: Optional[str] = None
    cuisine: Optional[str] = None
    why_hot: Optional[str] = None
    sources: Optional[List[str]] = None
    heat_score: int = 0
    res_difficulty: str = "Moderate"
    booking_tip: str = "Book ahead when possible; aim for off-peak times."
    notes: Optional[str] = None
    action_text: Optional[str] = None      # e.g., "Walk-ins only" (suppresses big button when no reserve_url)


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
        "User-Agent": "nyc-heat-index-bot/3.6 (+https://github.com/)",
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
# Text / name heuristics
# ---------------------------

_BAD_NAME_FRAGMENTS = [
    "newest restaurant openings", "now on resy", "newsletter", "sign up", "subscribe",
    "follow us", "gift card", "private dining", "more from resy",
    "read more", "updated", "where to eat", "the hit list", "the heatmap",
]

_REJECT_EXACT = {
    "about", "careers", "nearby restaurants", "top rated", "new on resy", "events",
    "features", "plans & pricing", "why resy os", "request a demo", "resy help desk",
    "global privacy policy", "terms of service", "cookie policy", "accessibility statement",
    "resy os overview", "resy os dashboard", "for restaurants", "resy", "get resy emails",
    "the resy credit", "global dining access", "discover more", "craving something else",
    "recommended", "you might also like", "related",
}


def _looks_like_restaurant_name(name: str) -> bool:
    if not name:
        return False
    n = _strip_leading_numbering(name.strip())
    if len(n) < 2 or len(n) > 90:
        return False

    low = n.lower()
    if low in _REJECT_EXACT:
        return False

    if any(bad in low for bad in _BAD_NAME_FRAGMENTS):
        return False

    if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}$", low):
        return False

    if re.fullmatch(r"[0-9\s\-/,:.]+", n):
        return False

    return True


# ---------------------------
# Image helpers
# ---------------------------

def _img_src_from_tag(img: Tag) -> Optional[str]:
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


def _best_src_from_img(img: Tag) -> Optional[str]:
    srcset = img.get("srcset")
    if srcset:
        candidates: List[str] = []
        for part in srcset.split(","):
            url = part.strip().split(" ")[0].strip()
            if url:
                candidates.append(url)
        if candidates:
            return candidates[-1]
    return _img_src_from_tag(img)


def _src_from_source_tag(source: Tag) -> Optional[str]:
    srcset = source.get("srcset") or source.get("data-srcset")
    if not srcset:
        return None
    candidates: List[str] = []
    for part in srcset.split(","):
        url = part.strip().split(" ")[0].strip()
        if url:
            candidates.append(url)
    return candidates[-1] if candidates else None


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
    max_tags_to_scan: int = 260,
    stop_on_modules: bool = True,
) -> Optional[str]:
    name_norm = _norm_name(restaurant_name)
    candidates: List[Tuple[int, str]] = []

    def score_url(u: str, alt: str) -> int:
        u_low = u.lower()
        alt_norm = _norm_name(alt or "")
        score = 0

        # Prefer Vox/Eater upload domains heavily (alts often describe dishes, not restaurant)
        if ("platform.ny.eater.com/wp-content/uploads/" in u_low or
            "platform.eater.com/wp-content/uploads/" in u_low or
            "cdn.vox-cdn.com" in u_low):
            score += 80

        # Hard reject Resy social preview junk
        if "s3.amazonaws.com/resy.com/images/social/" in u_low or "facebook-preview" in u_low:
            score -= 5000

        if name_norm and name_norm in alt_norm:
            score += 30

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
# Eater: Walk-ins + link precedence
# ---------------------------

def _extract_walkins_only(nodes: List[Tag]) -> bool:
    text_blob = " ".join([n.get_text(" ", strip=True) for n in nodes if isinstance(n, Tag)]).lower()
    patterns = [
        "walk ins only", "walk-ins only", "walk in only", "walk-in only",
        "walkins only",
    ]
    return any(p in text_blob for p in patterns)


def _pick_eater_links(nodes: List[Tag], base_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (reserve_url, website_url, venue_url)

    Rules:
    - reserve_url: only if domain is booking-ish AND link label looks like booking
    - website_url: explicit "Visit website" (or "Website")
    - venue_url: ny.eater.com/venue/ link
    - ignore Google Maps links for canonical links
    """
    reserve_url = None
    website_url = None
    venue_url = None

    for node in nodes:
        if not isinstance(node, Tag):
            continue

        for a in node.find_all("a", href=True):
            href = _abs_url(base_url, a["href"])
            if not href or not href.startswith("http"):
                continue

            hlow = href.lower()
            text = a.get_text(" ", strip=True).lower()
            aria = (a.get("aria-label") or "").lower()

            # Venue
            if "ny.eater.com/venue/" in hlow:
                venue_url = venue_url or href
                continue

            # Explicit “Visit website”
            if "visit website" in text or text == "website":
                website_url = website_url or href
                continue

            # Never treat maps as primary details
            if "google.com/maps" in hlow or "maps.google.com" in hlow:
                continue

            # Booking: only if booking-ish domain AND label indicates booking
            is_booking_domain = any(x in hlow for x in [
                "resy.com", "opentable.com", "exploretock.com", "reservations.safegraph.com"
            ])
            looks_like_booking = any(k in text for k in ["book", "reserve", "table"]) or any(k in aria for k in ["book", "reserve", "table"])

            if is_booking_domain and looks_like_booking:
                reserve_url = reserve_url or href
                continue

    return reserve_url, website_url, venue_url


# ---------------------------
# Resy: Hit List extractor based on article.venue2 blocks
# ---------------------------

def extract_resy_hit_list(html: str) -> List[Restaurant]:
    base_url = "https://blog.resy.com"
    soup = BeautifulSoup(html, "lxml")

    restaurants: List[Restaurant] = []

    # Hit List numbered entries are rendered as <article class="venue2">
    articles = soup.select("article.venue2")
    if not articles:
        articles = soup.select(".grid2-entry article")

    for art in articles:
        if not isinstance(art, Tag):
            continue

        name_tag = art.select_one(".venue2-name")
        if not name_tag:
            continue
        name = _strip_leading_numbering(name_tag.get_text(" ", strip=True))
        if not _looks_like_restaurant_name(name):
            continue
        if name.lower() in _REJECT_EXACT:
            continue

        # Neighborhood
        neighborhood = None
        loc = art.select_one(".venue2-location")
        if loc:
            neighborhood = loc.get_text(" ", strip=True) or None

        # Blurb
        why = None
        lead = art.select_one(".venue2-lead p")
        if lead:
            txt = lead.get_text(" ", strip=True)
            if txt and len(txt) >= 40:
                why = txt

        # Reserve URL: prefer the venue link in the title (booking-button href is often empty)
        reserve_url = None
        a_venue = art.select_one(".venue2-title a[href]")
        if a_venue:
            href = (a_venue.get("href") or "").strip()
            if href.startswith("http"):
                reserve_url = href

        # Image: only if it is inside the same article
        image_url = None
        img = art.select_one("figure.venue2-image img")
        if img:
            src = _best_src_from_img(img)
            if src:
                u = _abs_url(base_url, src)
                if u:
                    ulow = u.lower()
                    # Accept ONLY images owned by the Hit List page (WP uploads) or resy image CDN
                    if ("blog.resy.com/wp-content/uploads/" in ulow) or ulow.startswith("https://image.resy.com/"):
                        image_url = u

        restaurants.append(Restaurant(
            name=name,
            url=reserve_url,
            reserve_url=reserve_url,
            image_url=image_url,
            neighborhood=neighborhood,
            why_hot=why,
            sources=["Resy"],
        ))

    # resy page sometimes contains duplicates in mobile/desktop blocks; dedupe by name
    return dedupe(restaurants)


# ---------------------------
# Eater extractor
# ---------------------------

def _collect_between(start: Tag, end: Optional[Tag]) -> List[Tag]:
    nodes: List[Tag] = []
    for el in start.next_elements:
        if el is end:
            break
        if isinstance(el, Tag):
            nodes.append(el)
    return nodes


def extract_eater_heatmap(html: str) -> List[Restaurant]:
    base_url = "https://ny.eater.com"
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find("main") or soup

    restaurants: List[Restaurant] = []
    headings = article.find_all(["h2", "h3"])

    for i, h in enumerate(headings):
        title = _strip_leading_numbering(h.get_text(" ", strip=True))
        if not _looks_like_restaurant_name(title):
            continue

        low = title.lower()
        if low in ("see more", "more maps in eater ny") or low.startswith("more maps"):
            continue
        if any(k in low for k in ["related", "updates"]):
            continue

        name = title
        end = headings[i + 1] if i + 1 < len(headings) else None
        nodes = _collect_between(h, end)

        # why_hot: first substantial paragraph after heading
        why = None
        for node in nodes:
            if not isinstance(node, Tag):
                continue
            p = node if node.name == "p" else node.find("p")
            if p:
                txt = p.get_text(" ", strip=True)
                if txt and len(txt) >= 40:
                    why = txt
                    break

        image_url = _pick_image_from_slice(
            nodes,
            base_url=base_url,
            restaurant_name=name,
            max_tags_to_scan=260,
            stop_on_modules=True,
        )

        reserve_url, website_url, venue_url = _pick_eater_links(nodes, base_url=base_url)

        # Canonical details url (non-booking):
        canonical_url = venue_url or website_url or reserve_url

        walkins_only = _extract_walkins_only(nodes)
        action_text = None
        if walkins_only and not reserve_url:
            action_text = "Walk-ins only"

        restaurants.append(Restaurant(
            name=name,
            url=canonical_url,
            reserve_url=reserve_url,
            image_url=image_url,
            why_hot=why,
            sources=["Eater"],
            action_text=action_text,
        ))

    return dedupe(restaurants)


# ---------------------------
# Dedupe / merge preference
# ---------------------------

def dedupe(items: List[Restaurant]) -> List[Restaurant]:
    seen: Dict[str, Restaurant] = {}

    for r in items:
        r.name = _strip_leading_numbering(r.name)
        key = _norm_name(r.name)
        if not key:
            continue

        if key not in seen:
            seen[key] = r
            continue

        e = seen[key]
        e.sources = sorted(list(set((e.sources or []) + (r.sources or []))))

        # booking link: keep first non-empty (prefer exists)
        e.reserve_url = e.reserve_url or r.reserve_url

        # canonical url: keep existing unless empty
        e.url = e.url or r.url

        # blurb: keep existing unless empty
        e.why_hot = e.why_hot or r.why_hot

        # neighborhood/cuisine
        e.neighborhood = e.neighborhood or r.neighborhood
        e.cuisine = e.cuisine or r.cuisine

        # Image preference: if either item is from Eater and has an image, prefer it
        def is_eater_with_img(x: Restaurant) -> bool:
            return bool(x.sources and "Eater" in (x.sources or []) and x.image_url)

        if is_eater_with_img(r):
            e.image_url = r.image_url
        elif not e.image_url:
            e.image_url = r.image_url or e.image_url

        # Action text: if either says walk-ins only and we have no booking link, keep it
        if (e.action_text or r.action_text) and not e.reserve_url:
            e.action_text = "Walk-ins only"

    return list(seen.values())


# ---------------------------
# Heat score + reservation intel
# ---------------------------

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
    target = r.reserve_url or r.url or ""
    if "resy.com" in target:
        platform = "Resy"
    elif "opentable.com" in target:
        platform = "OpenTable"
    elif "exploretock.com" in target:
        platform = "Tock"

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

    if r.action_text and "walk" in r.action_text.lower():
        tip = "Treat it like a walk-in: go early (before 6pm) or late, and be flexible on party size."
        return diff, tip

    if platform == "Resy" or "resy" in text:
        tip = "Use Resy: check common drop times (often mornings), enable notifications, and target bar/counter seats."
    elif platform == "OpenTable" or "opentable" in text:
        tip = "Use OpenTable: book 1–2 weeks out, then set alerts for earlier times and watch for last-minute openings."
    elif platform == "Tock" or "tock" in text:
        tip = "Use Tock: look for ticketed times, cancellations, and last-minute releases."
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


# ---------------------------
# Optional: cache Resy images for GitHub Pages reliability
# ---------------------------

def _slugify(s: str) -> str:
    s = _norm_name(s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:80] or "img"


def _cache_remote_image(url: str, out_path: str) -> bool:
    try:
        headers = {
            "User-Agent": "nyc-heat-index-bot/3.6 (+https://github.com/)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://blog.resy.com/",
        }
        r = requests.get(url, headers=headers, timeout=(15, 45), allow_redirects=True)
        if r.status_code != 200 or not r.content:
            return False

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception:
        return False


def cache_resy_images_for_pages(restaurants: List[Restaurant], dist_dir: str) -> None:
    assets_dir = os.path.join(dist_dir, "assets")
    cached = 0
    attempted = 0

    for r in restaurants:
        if not r.sources or "Resy" not in r.sources:
            continue
        if r.sources and "Eater" in r.sources:
            continue  # don't touch Eater-preferred cards
        if not r.image_url:
            continue

        u_low = r.image_url.lower()
        # cache only resy-owned images we picked from the hit list article
        if not (u_low.startswith("https://image.resy.com/") or "blog.resy.com/wp-content/uploads/" in u_low):
            continue

        attempted += 1
        ext = ".jpg"
        if u_low.endswith(".png"):
            ext = ".png"
        fname = f"resy-{_slugify(r.name)}{ext}"
        out_path = os.path.join(assets_dir, fname)

        ok = _cache_remote_image(r.image_url, out_path)
        if ok:
            cached += 1
            if PUBLIC_BASE_URL:
                r.image_url = f"{PUBLIC_BASE_URL}/assets/{fname}"
            else:
                r.image_url = f"assets/{fname}"
        else:
            r.image_url = DEFAULT_RESY_IMAGE or None

    print(f"[ResyCache] attempted={attempted} cached={cached} public_base_set={bool(PUBLIC_BASE_URL)}")


# ---------------------------
# Render + email
# ---------------------------

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


# ---------------------------
# Main
# ---------------------------

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

    # Default Resy image for Resy-only cards with no Hit List photo
    if DEFAULT_RESY_IMAGE:
        for r in merged:
            is_resy_only = r.sources and ("Resy" in r.sources) and ("Eater" not in r.sources)
            if is_resy_only and not r.image_url:
                r.image_url = DEFAULT_RESY_IMAGE

    compute_heat(merged, last_month_names)

    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")
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
