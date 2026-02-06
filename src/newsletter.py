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

def _norm_name(name: str) -> str:
    n = name.lower().strip()
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
        "User-Agent": "nyc-heat-index-bot/1.6 (+https://github.com/)",
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
# Robust entry slicing (between headings in document order)
# ---------------------------

def _collect_between(start: Tag, end: Optional[Tag]) -> List[Tag]:
    """
    Collect Tag nodes in document order after `start` up to (but not including) `end`.
    Works even when the next heading isn't a sibling.
    """
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


def _pick_image_from_slice(nodes: List[Tag], base_url: str, restaurant_name: str) -> Optional[str]:
    """
    Choose best image inside the entry slice only (prevents mismatched images).
    """
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

    for node in nodes:
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
    # avoid generic OG images from source/listing pages
    if "eater.com" in u:
        return False
    if "blog.resy.com" in u:
        return False
    return True


# ---------------------------
# Source extractors
# ---------------------------

def extract_resy_hit_list(html: str) -> List[Restaurant]:
    base_url = "https://blog.resy.com"
    base_domain = "resy.com"

    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup

    restaurants: List[Restaurant] = []
    headings = article.find_all(["h2", "h3"])

    for i, h in enumerate(headings):
        text = h.get_text(" ", strip=True)
        if not text:
            continue

        if len(text) > 90:
            continue

        low = text.lower()
        if any(k in low for k in ["hit list", "where to eat", "updated", "read more", "the hit list"]):
            continue

        # promo / utility headings
        if any(phrase in low for phrase in [
            "newest restaurant openings",
            "openings, now on resy",
            "now on resy",
            "newsletter",
            "sign up",
            "subscribe",
            "follow us",
            "gift card",
            "resy events",
            "private dining",
            "more from resy",
        ]):
            continue

        name = re.sub(r"^\s*\d+\.\s*", "", text).strip()
        if len(name) < 2:
            continue

        nodes = _entry_slice_nodes_from_list(headings, i)

        why = _first_paragraph_from_slice(nodes)
        image_url = _pick_image_from_slice(nodes, base_url=base_url, restaurant_name=name)
        url = _pick_link_from_slice(nodes, base_url=base_url, base_domain=base_domain)

        # Entry gate: require at least a blurb OR a useful link
        # (images can be missing in static HTML)
        if not (why or url):
            continue

        if not url:
            a = h.find("a", href=True)
            url = _abs_url(base_url, a["href"]) if a else None

        restaurants.append(
            Restaurant(
                name=name,
                url=url,
                image_url=image_url,
                why_hot=why,
                sources=["Resy"],
            )
        )

    return dedupe(restaurants)


def extract_eater_heatmap(html: str) -> List[Restaurant]:
    base_url = "https://ny.eater.com"
    base_domain = "eater.com"

    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup

    restaurants: List[Restaurant] = []
    headings = article.find_all(["h2", "h3"])

    for i, h in enumerate(headings):
        title = h.get_text(" ", strip=True)
        if not title:
            continue

        low = title.lower()

        if len(title) > 110:
            continue
        if any(k in low for k in ["the heatmap", "where to eat", "related", "updates"]):
            continue

        if low in ("see more", "more maps in eater ny") or low.startswith("more maps"):
            continue

        name = title.strip()
        nodes = _entry_slice_nodes_from_list(headings, i)

        why = _first_paragraph_from_slice(nodes)
        image_url = _pick_image_from_slice(nodes, base_url=base_url, restaurant_name=name)
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

    return dedupe(restaurants)


# ---------------------------
# Merge / scoring / output
# ---------------------------

def dedupe(items: List[Restaurant]) -> List[Restaurant]:
    seen: Dict[str, Restaurant] = {}
    for r in items:
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
    print("Images:", sum(1 for r in merged if r.image_url), "of", len(merged))


if __name__ == "__main__":
    main()
