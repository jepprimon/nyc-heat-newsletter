from dataclasses import dataclass

@dataclass(frozen=True)
class Source:
    name: str
    url: str

# Sources (as of Feb 2026)
SOURCES = [
    Source(
        name="Resy Hit List (NYC)",
        url="https://blog.resy.com/the-hit-list/nyc-restaurants/",
    ),
    Source(
        name="Eater Heatmap (Manhattan)",
        url="https://ny.eater.com/maps/best-new-nyc-restaurants-heatmap",
    ),
]

# Scoring weights (tune as you like)
WEIGHTS = {
    "both_sources_bonus": 40,
    "new_this_month_bonus": 20,
    "carried_over_bonus": 10,
    "language_intensity_max": 15,
    "reservation_scarcity_max": 15,
}

# Keyword buckets (used for intensity + scarcity heuristics)
INTENSITY_KEYWORDS = {
    5: ["buzz", "buzzy", "hype", "hot", "hottest", "must-try", "viral"],
    8: ["line", "lines", "packed", "crowded", "always full", "slam", "slammed"],
    12: ["hard to book", "tough reservation", "impossible", "sold out", "booked up"],
    15: ["the hardest", "nearly impossible", "months out"],
}

SCARCITY_KEYWORDS = {
    5: ["reservations recommended", "book ahead", "limited seating"],
    8: ["reservation release", "drops", "at noon", "at 10am", "set your alarm"],
    12: ["walk-in only", "walk ins only", "no reservations", "bar seats", "counter seats"],
    15: ["ticketed", "prepaid", "waiting list"],
}
