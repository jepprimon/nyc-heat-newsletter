import os
import re
import json
import smtplib
import ssl
import time
import random
import requests
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import tz
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import SOURCES, WEIGHTS, INTENSITY_KEYWORDS, SCARCITY_KEYWORDS


@dataclass
class Restaurant:
    name: str
    url: Optional[str] = None
    neighborhood: Optional[str] = None
    cuisine: Optional[str] = None
    why_hot: Optional[str] = None
    sources: Optional[List[str]] = None
    heat_score: int = 0
    res_difficulty: str = "Moderate"
    booking_tip: str = "Book ahead when possible; aim for off-peak times."
    notes: Optional[str] = None


def _norm_name(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[\u2019’]", "'", n)
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"[^a-z0-9 '&-]", "", n)
    return n


def fetch_html(url: str, timeout: int = 45, retries: int = 4) -> str:
    headers = {
        "User-Agent": "nyc-heat-index-bot/1.0 (+https://github.com/)",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "close",
    }

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # Separate connect and read timeouts: (connect, read)
            r = requests.get(url, headers=headers, timeout=(15, timeout))
            r.raise_for_status()
            return r.text
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError) as e:
            last_err = e
            # Exponential backoff + jitter
            sleep_s = min(60, (2 ** (attempt - 1)) * 2) + random.random()
            print(f"Fetch failed ({attempt}/{retries}) for {url}: {e}. Retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)

    raise last_err


def extract_resy_hit_list(html: str) -> List[Restaurant]:
    '''Extract restaurant headings + a nearby paragraph from the Resy Hit List NYC page.'''
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup

    restaurants: List[Restaurant] = []
    headings = article.find_all(["h2", "h3"])

    for h in headings:
        text = h.get_text(" ", strip=True)
        if not text:
            continue
        if len(text) > 80:
            continue
        if any(k in text.lower() for k in ["where", "hit list", "updated", "read more"]):
            continue

        p = h.find_next("p")
        why = p.get_text(" ", strip=True) if p else None

        link = h.find("a")
        url = link.get("href") if link else None
        if url and url.startswith("/"):
            url = "https://blog.resy.com" + url

        name = re.sub(r"^\s*\d+\.\s*", "", text).strip()
        if len(name) < 2:
            continue

        restaurants.append(Restaurant(name=name, url=url, why_hot=why, sources=["Resy"]))

    return dedupe(restaurants)


def extract_eater_heatmap(html: str) -> List[Restaurant]:
    '''Extract restaurant headings + a nearby paragraph from an Eater heatmap page.'''
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup

    restaurants: List[Restaurant] = []
    for h in article.find_all(["h2", "h3"]):
        title = h.get_text(" ", strip=True)
        if not title or len(title) > 90:
            continue
        if any(k in title.lower() for k in ["map", "heatmap", "editors", "updated", "related"]):
            continue

        p = h.find_next("p")
        why = p.get_text(" ", strip=True) if p else None

        a = h.find("a")
        url = a.get("href") if a else None

        restaurants.append(Restaurant(name=title.strip(), url=url, why_hot=why, sources=["Eater"]))

    return dedupe(restaurants)


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
        tip = "Use OpenTable: book 1–2 weeks out, then set alerts for earlier times and watch for last‑minute openings."
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
    tz_name = os.environ.get("TIMEZONE", "America/New_York")
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

    issue_slug = now_local.strftime("%Y-%m")
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"issue-{issue_slug}.html")
    index_path = os.path.join(output_dir, "index.html")
    
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    return title, out_path


def send_email(subject: str, html_body: str) -> None:
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    subscribers = [e.strip() for e in os.environ.get("SUBSCRIBERS", "").split(",") if e.strip()]
    if not subscribers:
        raise RuntimeError("SUBSCRIBERS secret is empty. Provide comma-separated emails.")

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("FROM_EMAIL", username)
    from_name = os.environ.get("FROM_NAME", "NYC Heat Index")
    reply_to = os.environ.get("REPLY_TO", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = from_email
    msg["Bcc"] = ", ".join(subscribers)
    if reply_to:
        msg["Reply-To"] = reply_to

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if dry_run:
        print("DRY_RUN enabled: not sending email. Would have sent to:", len(subscribers), "subscribers")
        return

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

        all_items.extend(items)

    merged = dedupe(all_items)
    for r in merged:
        r.sources = sorted(list(set(r.sources or [])))

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


if __name__ == "__main__":
    main()
