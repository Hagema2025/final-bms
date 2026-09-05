"""
BMS Ticket Checker
------------------

Checks multiple BookMyShow watches defined in watches.json.

Each watch can have:
    - name
    - url
    - dates
    - theatre
    - time_period
    - discover_variants   (bool, optional, default false)
    - languages           (optional whitelist, list or comma string)
    - formats             (optional whitelist, list or comma string)

When discover_variants is true, the checker reads the "Select language
and format" chips returned by BMS for the watch's URL/eventCode, and
also checks every other language/format variant of the same movie
(e.g. Tamil 2D, Tamil EPIQ, Telugu 2D, ...), each as its own tracked
sub-watch with its own state and its own change/email alerts.

If "languages" and/or "formats" are given, only variants whose
language/format match one of the given values (case-insensitive) are
additionally tracked; the watch's own URL/eventCode is always tracked
regardless of these lists.

Example watches.json:

[
  {
    "name": "Immortal",
    "url": "https://in.bookmyshow.com/movies/chennai/immortal/ET00513702",
    "dates": ["20260905", "20260906"],
    "theatre": "",
    "time_period": "",
    "discover_variants": true,
    "languages": ["Tamil"],
    "formats": []
  }
]

Email configuration is read from environment variables:

    RESEND_API_KEY
    RESEND_TO_EMAIL
    RESEND_FROM_EMAIL

State is stored in:

    bms_state.json
"""

import os
import re
import sys
import json
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests


# ======================================================================
# CONFIGURATION
# ======================================================================

WATCHES_FILE = "data/watches.json"
STATE_FILE = "data/bms_state.json"

# RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
# RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
# RESEND_FROM_EMAIL = os.getenv(
#     "RESEND_FROM_EMAIL",
#     "onboarding@resend.dev"
# )

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ======================================================================
# CONSTANTS
# ======================================================================

AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default": "AVAILABLE",
}

TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}

REGION_MAP = {
    "chennai": (
        "CHEN",
        "chennai",
        "13.056",
        "80.206",
        "tf3",
    ),
    "mumbai": (
        "MUMBAI",
        "mumbai",
        "19.076",
        "72.878",
        "te7",
    ),
    "delhi-ncr": (
        "NCR",
        "delhi-ncr",
        "28.613",
        "77.209",
        "ttn",
    ),
    "delhi": (
        "NCR",
        "delhi-ncr",
        "28.613",
        "77.209",
        "ttn",
    ),
    "bengaluru": (
        "BANG",
        "bengaluru",
        "12.972",
        "77.594",
        "tdr",
    ),
    "bangalore": (
        "BANG",
        "bengaluru",
        "12.972",
        "77.594",
        "tdr",
    ),
    "hyderabad": (
        "HYD",
        "hyderabad",
        "17.385",
        "78.487",
        "tep",
    ),
    "kolkata": (
        "KOLK",
        "kolkata",
        "22.573",
        "88.364",
        "tun",
    ),
    "pune": (
        "PUNE",
        "pune",
        "18.520",
        "73.856",
        "te2",
    ),
    "kochi": (
        "KOCH",
        "kochi",
        "9.932",
        "76.267",
        "t9z",
    ),
}


API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


# ======================================================================
# DATA CLASSES
# ======================================================================

@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)


@dataclass
class DateInfo:
    date_code: str
    status: str


@dataclass
class VariantInfo:
    language: str
    format: str
    event_code: str
    event_url: str
    is_current: bool


# ======================================================================
# WATCH CONFIGURATION
# ======================================================================

def _as_list(value):
    """
    Normalize a config value that may be a list, a comma-separated
    string, or missing, into a clean list of stripped strings.
    """

    if not value:
        return []

    if isinstance(value, str):
        return [
            item.strip()
            for item in value.split(",")
            if item.strip()
        ]

    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    return []


def load_watches():
    """
    Load watches from watches.json.
    """

    if not os.path.exists(WATCHES_FILE):
        print(f"❌ {WATCHES_FILE} not found.")
        sys.exit(1)

    try:
        with open(WATCHES_FILE, "r", encoding="utf-8") as f:
            watches = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in {WATCHES_FILE}")
        print(f"   {e}")
        sys.exit(1)

    if not isinstance(watches, list):
        print(f"❌ {WATCHES_FILE} must contain a JSON array.")
        sys.exit(1)

    if not watches:
        print(f"❌ No watches configured in {WATCHES_FILE}.")
        sys.exit(1)

    validated = []

    for index, watch in enumerate(watches, start=1):

        if not isinstance(watch, dict):
            print(f"❌ Watch #{index} must be an object.")
            sys.exit(1)

        name = str(watch.get("name", "")).strip()
        url = str(watch.get("url", "")).strip()

        if not name:
            print(f"❌ Watch #{index} is missing 'name'.")
            sys.exit(1)

        if not url:
            print(f"❌ Watch #{index} is missing 'url'.")
            sys.exit(1)

        dates = watch.get("dates", [])

        if isinstance(dates, str):
            dates = [
                d.strip()
                for d in dates.split(",")
                if d.strip()
            ]

        if not isinstance(dates, list):
            print(
                f"❌ Watch '{name}': 'dates' must be "
                f"an array or comma-separated string."
            )
            sys.exit(1)

        dates = [str(d).strip() for d in dates if str(d).strip()]

        theatre = [
            t.lower()
            for t in _as_list(watch.get("theatre"))
        ]

        

        time_period = [
            tp.lower()
            for tp in _as_list(watch.get("time_period"))
        ]

        discover_variants = bool(
            watch.get("discover_variants", False)
        )

        languages = [
            lang.lower()
            for lang in _as_list(watch.get("languages"))
        ]

        formats = [
            fmt.lower()
            for fmt in _as_list(watch.get("formats"))
        ]

        validated.append({
            "name": name,
            "url": url,
            "dates": dates,
            "theatre": theatre,
            "time_period": time_period,
            "discover_variants": discover_variants,
            "languages": languages,
            "formats": formats,
        })

    return validated


# ======================================================================
# URL PARSER
# ======================================================================

def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    result = {
        "event_code": None,
        "date_code": None,
        "region_slug": None,
    }

    for part in parts:

        if re.match(r"^ET\d{8,}$", part):
            result["event_code"] = part

        elif re.match(r"^\d{8}$", part):
            result["date_code"] = part

    if "movies" in parts:

        idx = parts.index("movies")

        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]

    return result


# ======================================================================
# REGION RESOLVER
# ======================================================================

def resolve_region(slug):

    key = (slug or "").lower().strip()

    if key in REGION_MAP:
        return REGION_MAP[key]

    return (
        key.upper()[:6],
        key,
        "0",
        "0",
        "",
    )


# ======================================================================
# BMS API
# ======================================================================

def fetch_bms(
    event_code,
    date_code,
    region_code,
    region_slug,
    lat,
    lon,
    geohash,
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",

        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),

        "sec-ch-ua": (
            '"Chromium";v="145", '
            '"Not:A-Brand";v="99"'
        ),

        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',

        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }

    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": lat,
        "lon": lon,
    }

    try:

        response = requests.get(
            API_URL,
            headers=headers,
            params=params,
            timeout=20,
        )

        if response.status_code == 200:
            return response.json()

        print(
            f"  ⚠️ BMS HTTP {response.status_code}"
        )

    except requests.RequestException as e:

        print(
            f"  ⚠️ BMS request failed: {e}"
        )

    return None


# ======================================================================
# MOVIE INFO PARSER
# ======================================================================

def parse_movie_info(data):

    info = {
        "name": "Unknown Movie",
        "language": "",
    }

    for widget in data.get(
        "data", {}
    ).get(
        "topStickyWidgets", []
    ):

        if widget.get("type") != "horizontal-text-list":
            continue

        for item in widget.get("data", []):

            for row in item.get(
                "leftText", {}
            ).get(
                "data", []
            ):

                for component in row.get(
                    "components", []
                ):

                    text = component.get("text", "")

                    if "•" in text:
                        info["language"] = text.strip()

    bottom_sheet = (
        data.get("data", {})
        .get("bottomSheetData", {})
    )

    for widget in (
        bottom_sheet
        .get("format-selector", {})
        .get("widgets", [])
    ):

        if widget.get("type") != "vertical-text-list":
            continue

        for item in widget.get("data", []):

            if item.get("styleId") == "bottomsheet-subtitle":

                info["name"] = item.get(
                    "text",
                    info["name"],
                )

    return info


# ======================================================================
# LANGUAGE / FORMAT VARIANT PARSER
# ======================================================================

def parse_format_selector(data):
    """
    Read the "Select language and format" bottomsheet and return
    one VariantInfo per selectable language+format chip, including
    the currently-selected one (isDisabled == true).
    """

    variants = []

    bottom_sheet = (
        data.get("data", {})
        .get("bottomSheetData", {})
    )

    widgets = (
        bottom_sheet
        .get("format-selector", {})
        .get("widgets", [])
    )

    for widget in widgets:

        if widget.get("type") != "chip-list":
            continue

        # The chip-list "text" field holds the language name
        # (e.g. "Tamil", "Malayalam") for that group of chips.
        language = widget.get("text", "").strip()

        for chip in widget.get("data", []):

            if chip.get("type") != "chip":
                continue

            cta = chip.get("cta", {})
            additional = cta.get("additionalData", {})

            event_code = additional.get("eventCode", "")

            if not event_code:
                continue

            variants.append(
                VariantInfo(
                    language=(
                        additional.get(
                            "language",
                            language,
                        )
                        or language
                    ),
                    format=chip.get("title", "").strip(),
                    event_code=event_code,
                    event_url=additional.get(
                        "eventUrl",
                        "",
                    ),
                    is_current=bool(
                        additional.get(
                            "isDisabled",
                            False,
                        )
                    ),
                )
            )

    return variants


def select_variants(variants, languages, formats):
    """
    Filter discovered variants (excluding the current/base one) down
    to those matching the languages/formats whitelists, if given.
    Empty whitelist means "match everything".
    """

    result = []

    for variant in variants:

        if variant.is_current:
            continue

        if languages and variant.language.lower() not in languages:
            continue

        if formats and variant.format.lower() not in formats:
            continue

        result.append(variant)

    return result


# ======================================================================
# DATE PARSER
# ======================================================================

def parse_dates(data):

    dates = []

    widgets = (
        data.get("data", {})
        .get("topStickyWidgets", [])
    )

    for widget in widgets:

        if widget.get(
            "type"
        ) != "horizontal-block-list":
            continue

        for item in widget.get("data", []):

            texts = item.get("data", [])

            if len(texts) < 3:
                continue

            style = item.get(
                "styleId",
                "",
            )

            dates.append(
                DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(
                        style,
                        "UNKNOWN",
                    ),
                )
            )

    return dates


# ======================================================================
# SHOW PARSER
# ======================================================================

def parse_shows(data):

    shows = []

    widgets = (
        data.get("data", {})
        .get("showtimeWidgets", [])
    )

    for widget in widgets:

        if widget.get(
            "type"
        ) != "groupList":
            continue

        for group in widget.get("data", []):

            if group.get(
                "type"
            ) != "venueGroup":
                continue

            for card in group.get("data", []):

                if card.get(
                    "type"
                ) != "venue-card":
                    continue

                additional = card.get(
                    "additionalData",
                    {},
                )

                venue_name = additional.get(
                    "venueName",
                    "Unknown",
                )

                venue_code = additional.get(
                    "venueCode",
                    "",
                )

                for showtime in card.get(
                    "showtimes",
                    [],
                ):

                    show_additional = (
                        showtime.get(
                            "additionalData",
                            {},
                        )
                    )

                    date_code = str(
                        show_additional.get(
                            "showDateCode",
                            "",
                        )
                        or show_additional.get(
                            "dateCode",
                            "",
                        )
                    ).strip()

                    cutoff = show_additional.get(
                        "cutOffDateTime",
                        "",
                    )

                    if (
                        not date_code
                        and re.match(
                            r"^\d{8}",
                            cutoff,
                        )
                    ):
                        date_code = cutoff[:8]

                    show = ShowInfo(
                        venue_code=venue_code,
                        venue_name=venue_name,
                        session_id=show_additional.get(
                            "sessionId",
                            "",
                        ),
                        date_code=date_code,
                        time=showtime.get(
                            "title",
                            "",
                        ),
                        time_code=show_additional.get(
                            "showTimeCode",
                            "",
                        ),
                        screen_attr=(
                            showtime.get(
                                "screenAttr",
                                "",
                            )
                            or show_additional.get(
                                "attributes",
                                "",
                            )
                        ),
                    )

                    for category in show_additional.get(
                        "categories",
                        [],
                    ):

                        status = str(
                            category.get(
                                "availStatus",
                                "",
                            )
                        )

                        show.categories.append(
                            CatInfo(
                                name=category.get(
                                    "priceDesc",
                                    "",
                                ),
                                price=str(
                                    category.get(
                                        "curPrice",
                                        "0",
                                    )
                                ),
                                status=status,
                            )
                        )

                    shows.append(show)

    return shows


# ======================================================================
# FILTERING
# ======================================================================

def filter_shows(
    shows,
    theatre_filter,
    time_periods,
    date_codes,
):

    result = []

    theatre_keywords = theatre_filter if theatre_filter else []
    periods = time_periods if time_periods else []

    dates_set = (
        set(
            d.strip()
            for d in date_codes
            if str(d).strip()
        )
        if date_codes
        else set()
    )

    for show in shows:

        # --------------------------------------------------------------
        # Theatre filter
        # --------------------------------------------------------------

        if theatre_keywords:

            venue_lower = (
                show.venue_name.lower()
            )

            if not any(
                keyword in venue_lower
                for keyword in theatre_keywords
            ):
                continue

        # --------------------------------------------------------------
        # Date filter
        # --------------------------------------------------------------

        if (
            dates_set
            and show.date_code
            and show.date_code not in dates_set
        ):
            continue

        # --------------------------------------------------------------
        # Time filter
        # --------------------------------------------------------------

        if periods:

            try:
                time_code = int(
                    show.time_code
                )
            except (ValueError, TypeError):
                time_code = 0

            matched = False

            for period in periods:

                if period not in TIME_PERIODS:
                    continue

                start, end = TIME_PERIODS[period]

                if start <= time_code < end:
                    matched = True
                    break

            if not matched:
                continue

        result.append(show)

    return result


# ======================================================================
# STATE
# ======================================================================

def load_state():

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            return json.load(f)

    except (
        FileNotFoundError,
        json.JSONDecodeError,
    ):

        return {}


def save_state(state):

    temp_file = f"{STATE_FILE}.tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False,
        )

    os.replace(
        temp_file,
        STATE_FILE,
    )


# ======================================================================
# STATE BUILDER
# ======================================================================

def build_state(
    shows,
    dates,
):

    show_state = {}

    for show in shows:

        for category in show.categories:

            key = (
                f"{show.venue_code}|"
                f"{show.session_id}|"
                f"{show.date_code}|"
                f"{category.name}"
            )

            show_state[key] = {
                "venue": show.venue_name,
                "time": show.time,
                "date": show.date_code,
                "cat": category.name,
                "price": category.price,
                "status": category.status,
                "screen": show.screen_attr,
            }

    date_state = {
        date.date_code: date.status
        for date in dates
    }

    return {
        "shows": show_state,
        "dates": date_state,
    }


# ======================================================================
# CHANGE DETECTION
# ======================================================================

def detect_changes(
    old_state,
    new_state,
):

    changes = []

    # --------------------------------------------------------------
    # New dates
    # --------------------------------------------------------------

    old_dates = old_state.get(
        "dates",
        {},
    )

    new_dates = new_state.get(
        "dates",
        {},
    )

    for date_code, status in new_dates.items():

        old_status = old_dates.get(
            date_code
        )

        if (
            old_status == "NOT_OPEN"
            and status in (
                "BOOKABLE",
                "AVAILABLE",
            )
        ):

            changes.append(
                f"📅 NEW DATE OPENED: {date_code}"
            )

    # --------------------------------------------------------------
    # Shows
    # --------------------------------------------------------------

    old_shows = old_state.get(
        "shows",
        {},
    )

    new_shows = new_state.get(
        "shows",
        {},
    )

    # --------------------------------------------------------------
    # New showtimes
    # --------------------------------------------------------------

    for key in (
        set(new_shows)
        - set(old_shows)
    ):

        show = new_shows[key]

        changes.append(
            f"🆕 NEW: "
            f"{show['venue']} "
            f"{show['time']} "
            f"[{show['date']}] "
            f"— {show['cat']} "
            f"₹{show['price']}"
        )

    # --------------------------------------------------------------
    # Sold out -> available
    # --------------------------------------------------------------

    for key, new_show in new_shows.items():

        old_show = old_shows.get(key)

        if not old_show:
            continue

        if (
            old_show["status"] == "0"
            and new_show["status"] != "0"
        ):

            label, icon = AVAIL_STATUS_MAP.get(
                new_show["status"],
                ("UNKNOWN", "⚪"),
            )

            changes.append(
                f"{icon} BACK: "
                f"{new_show['venue']} "
                f"{new_show['time']} "
                f"[{new_show['date']}] "
                f"— {new_show['cat']} "
                f"→ {label}"
            )

    # --------------------------------------------------------------
    # Availability changes
    # --------------------------------------------------------------

    for key, new_show in new_shows.items():

        old_show = old_shows.get(key)

        if not old_show:
            continue

        old_status = old_show["status"]
        new_status = new_show["status"]

        if (
            old_status != new_status
            and old_status != "0"
            and new_status != "0"
        ):

            old_label = AVAIL_STATUS_MAP.get(
                old_status,
                ("UNKNOWN", "⚪"),
            )[0]

            new_label, new_icon = AVAIL_STATUS_MAP.get(
                new_status,
                ("UNKNOWN", "⚪"),
            )

            changes.append(
                f"{new_icon} STATUS: "
                f"{new_show['venue']} "
                f"{new_show['time']} "
                f"[{new_show['date']}] "
                f"— {new_show['cat']} "
                f"{old_label} → {new_label}"
            )

    return changes


# ======================================================================
# EMAIL HELPERS
# ======================================================================

def category_status_label(status):

    return AVAIL_STATUS_MAP.get(
        status,
        ("UNKNOWN", ""),
    )[0]


# ======================================================================
# Telegram
# ======================================================================

def send_telegram(watch_name, subject, changes, shows, movie_info):
    """
    Sends a structured, email-like formatted alert via Telegram API.
    Handles message splitting automatically if the content exceeds character limits.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" ⚠️ Telegram skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", watch_name)

    def escape_md(text: str) -> str:
        """Escapes required MarkdownV2 reserve characters."""
        special_chars = r"_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{char}" if char in special_chars else char for char in str(text))

    # --------------------------------------------------------------
    # Header Section
    # --------------------------------------------------------------
    lines = [
        f"📩 *BMS Alert: {escape_md(movie_name)}*",
        f"🏷️ *Watch:* {escape_md(watch_name)}",
        f"🕒 _{escape_md(now_str)}_",
        "───────────────────────────",
    ]

    # --------------------------------------------------------------
    # Changes Section
    # --------------------------------------------------------------
    if changes:
        lines.append("\n⚡ *Changes Detected*")
        for change in changes:
            lines.append(f"• {escape_md(change)}")

    # --------------------------------------------------------------
    # Current Showtimes Section
    # --------------------------------------------------------------
    lines.append("\n🎬 *Current Showtimes*")

    venue_groups = {}
    for show in shows:
        venue_groups.setdefault(show.venue_name, []).append(show)

    for venue_name, venue_shows in venue_groups.items():
        lines.append(f"\n📍 *{escape_md(venue_name)}*")

        for show in venue_shows:
            categories_str = " \| ".join(
                f"{escape_md(cat.name)} Rs\\.{escape_md(cat.price)} \\({escape_md(category_status_label(cat.status))}\\)"
                for cat in show.categories
            )

            screen = f" \\[{escape_md(show.screen_attr)}\\]" if show.screen_attr else ""

            lines.append(
                f"`{escape_md(show.time)}`{screen} \\| _{escape_md(show.date_code)}_\n"
                f"└ {categories_str}"
            )

    lines.append("\n───────────────────────────")
    lines.append("🤖 _Automated BMS Ticket Notifier_")

    # --------------------------------------------------------------
    # Chunking & Delivery Logic (Limit Protection)
    # --------------------------------------------------------------
    full_message = "\n".join(lines)
    chunks = []
    
    # Telegram max limit is 4096; safe boundary set to 3800
    while len(full_message) > 3800:
        split_idx = full_message.rfind("\n\n", 0, 3800)
        if split_idx == -1:
            split_idx = full_message.rfind("\n", 0, 3800)
        
        chunks.append(full_message[:split_idx])
        full_message = full_message[split_idx:].lstrip()

    chunks.append(full_message)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for idx, chunk in enumerate(chunks):
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )

            if response.status_code == 200:
                print(f" ✅ Telegram message chunk {idx + 1}/{len(chunks)} sent.")
            else:
                print(f" ❌ Telegram API {response.status_code}: {response.text}")

        except requests.RequestException as e:
            print(f" ❌ Telegram notification failed: {e}")

# ======================================================================
# EMAIL
# ======================================================================

# def send_email(
#     watch_name,
#     subject,
#     changes,
#     shows,
#     movie_info,
# ):

#     api_key = RESEND_API_KEY.strip()
#     to = RESEND_TO_EMAIL.strip()

#     sender = (
#         RESEND_FROM_EMAIL.strip()
#         or "onboarding@resend.dev"
#     )

#     if not api_key or not to:

#         print(
#             "  ⚠️ Email skipped — "
#             "RESEND_API_KEY or RESEND_TO_EMAIL "
#             "not configured."
#         )

#         return

#     now_str = datetime.now().strftime(
#         "%d %b %Y, %I:%M %p"
#     )

#     movie_name = movie_info.get(
#         "name",
#         watch_name,
#     )

#     # --------------------------------------------------------------
#     # Changes
#     # --------------------------------------------------------------

#     changes_html = ""

#     if changes:

#         rows = "".join(
#             (
#                 '<li style="padding:3px 0;'
#                 'font-size:14px;">'
#                 f"{escape(change)}"
#                 "</li>"
#             )
#             for change in changes
#         )

#         changes_html = f"""
#         <h3 style="
#             margin:0 0 8px 0;
#             font-size:15px;
#             font-weight:bold;
#             color:#333;
#         ">
#             Changes Detected
#         </h3>

#         <ul style="
#             margin:0 0 20px 0;
#             padding-left:20px;
#             line-height:1.6;
#             color:#333;
#         ">
#             {rows}
#         </ul>
#         """

#     # --------------------------------------------------------------
#     # Group shows by venue
#     # --------------------------------------------------------------

#     venue_groups = {}

#     for show in shows:

#         venue_groups.setdefault(
#             show.venue_name,
#             [],
#         ).append(show)

#     shows_html = ""

#     for venue_name, venue_shows in venue_groups.items():

#         show_rows = ""

#         for show in venue_shows:

#             categories = " | ".join(
#                 (
#                     f"{escape(category.name)} "
#                     f"Rs.{escape(category.price)} "
#                     f"({category_status_label(category.status)})"
#                 )
#                 for category in show.categories
#             )

#             screen = (
#                 f" [{escape(show.screen_attr)}]"
#                 if show.screen_attr
#                 else ""
#             )

#             show_rows += f"""
#             <tr>
#                 <td style="
#                     padding:5px 8px;
#                     border-bottom:1px solid #ddd;
#                     font-size:13px;
#                     vertical-align:top;
#                 ">
#                     {escape(show.time)}
#                     {screen}
#                     <br>
#                     <span style="color:#777;">
#                         {escape(show.date_code)}
#                     </span>
#                 </td>

#                 <td style="
#                     padding:5px 8px;
#                     border-bottom:1px solid #ddd;
#                     font-size:13px;
#                     vertical-align:top;
#                 ">
#                     {categories}
#                 </td>
#             </tr>
#             """

#         shows_html += f"""
#         <p style="
#             margin:14px 0 4px 0;
#             font-size:14px;
#             font-weight:bold;
#             color:#333;
#         ">
#             {escape(venue_name)}
#         </p>

#         <table style="
#             width:100%;
#             border-collapse:collapse;
#             font-size:13px;
#         ">

#             <tr style="background:#f5f5f5;">

#                 <th style="
#                     padding:5px 8px;
#                     text-align:left;
#                     border-bottom:1px solid #ddd;
#                 ">
#                     Time
#                 </th>

#                 <th style="
#                     padding:5px 8px;
#                     text-align:left;
#                     border-bottom:1px solid #ddd;
#                 ">
#                     Categories
#                 </th>

#             </tr>

#             {show_rows}

#         </table>
#         """

#     # --------------------------------------------------------------
#     # HTML
#     # --------------------------------------------------------------

#     html = f"""
# <!doctype html>

# <html>

# <head>
#     <meta charset="utf-8">
# </head>

# <body style="
#     margin:0;
#     padding:24px;
#     font-family:Arial,Helvetica,sans-serif;
#     font-size:14px;
#     color:#333;
#     background:#fff;
# ">

#     <h2 style="
#         margin:0 0 4px 0;
#         font-size:18px;
#         color:#111;
#     ">
#         BMS Alert: {escape(movie_name)}
#     </h2>

#     <p style="
#         margin:0 0 4px 0;
#         font-size:13px;
#         color:#666;
#     ">
#         Watch: {escape(watch_name)}
#     </p>

#     <p style="
#         margin:0 0 20px 0;
#         font-size:13px;
#         color:#666;
#     ">
#         {escape(now_str)}
#     </p>

#     <hr style="
#         border:none;
#         border-top:1px solid #ddd;
#         margin:0 0 20px 0;
#     ">

#     {changes_html}

#     <h3 style="
#         margin:0 0 8px 0;
#         font-size:15px;
#         font-weight:bold;
#         color:#333;
#     ">
#         Current Showtimes
#     </h3>

#     {shows_html}

#     <p style="
#         margin:24px 0 0 0;
#         font-size:12px;
#         color:#999;
#     ">
#         Automated BMS Ticket Notifier
#     </p>

# </body>

# </html>
# """

#     # --------------------------------------------------------------
#     # Plain text
#     # --------------------------------------------------------------

#     plain_lines = [
#         subject,
#         "",
#         f"Watch: {watch_name}",
#         f"Checked at: {now_str}",
#         "",
#     ]

#     if changes:

#         plain_lines.append(
#             "Changes Detected:"
#         )

#         plain_lines.extend(
#             f"  - {change}"
#             for change in changes
#         )

#         plain_lines.append("")

#     plain_lines.append(
#         "Current Showtimes:"
#     )

#     for venue_name, venue_shows in venue_groups.items():

#         plain_lines.append(
#             f"\n{venue_name}"
#         )

#         for show in venue_shows:

#             categories = " | ".join(
#                 (
#                     f"{category.name} "
#                     f"Rs.{category.price} "
#                     f"({category_status_label(category.status)})"
#                 )
#                 for category in show.categories
#             )

#             screen = (
#                 f" [{show.screen_attr}]"
#                 if show.screen_attr
#                 else ""
#             )

#             plain_lines.append(
#                 f"  {show.date_code} "
#                 f"{show.time}"
#                 f"{screen} - "
#                 f"{categories}"
#             )

#     plain_lines.extend([
#         "",
#         "Automated BMS Ticket Notifier.",
#     ])

#     plain = "\n".join(
#         plain_lines
#     )

#     # --------------------------------------------------------------
#     # Send
#     # --------------------------------------------------------------

#     try:

#         response = requests.post(
#             "https://api.resend.com/emails",

#             headers={
#                 "Authorization": (
#                     f"Bearer {api_key}"
#                 ),
#                 "Content-Type": (
#                     "application/json"
#                 ),
#             },

#             json={
#                 "from": sender,
#                 "to": [to],
#                 "subject": subject,
#                 "text": plain,
#                 "html": html,
#             },

#             timeout=20,
#         )

#         if response.status_code in (
#             200,
#             201,
#         ):

#             print(
#                 f"  ✅ Email sent to {to}"
#             )

#         else:

#             print(
#                 f"  ❌ Resend "
#                 f"{response.status_code}: "
#                 f"{response.text}"
#             )

#     except requests.RequestException as e:

#         print(
#             f"  ❌ Email failed: {e}"
#         )


# ======================================================================
# RUN A SINGLE EVENT (one language/format variant, or the base watch)
# ======================================================================

def run_event(
    label,
    event_code,
    region_code,
    region_slug_resolved,
    lat,
    lon,
    geohash,
    date_list,
    theatre,
    time_period,
    dates_filter,
    state,
    save_raw_prefix=None,
):
    """
    Fetch, filter, diff and (if needed) alert for one event_code.

    Returns (state, success, first_full_data) where first_full_data
    is the first raw BMS response fetched (used for variant
    discovery when called for the base watch), or None if nothing
    was fetched successfully.
    """

    all_shows = []
    all_dates = []
    first_full_data = None

    movie_info = {
        "name": label,
        "language": "",
    }

    for date_code in date_list:

        print(
            f"  🔎 [{label}] Checking date "
            f"{date_code or '(default)'}..."
        )

        data = fetch_bms(
            event_code,
            date_code,
            region_code,
            region_slug_resolved,
            lat,
            lon,
            geohash,
        )

        if data:

            if first_full_data is None:
                first_full_data = data

            if save_raw_prefix:

                filename = (
                    f"{save_raw_prefix}_"
                    f"{date_code or 'default'}.json"
                )

                with open(
                    filename, "w", encoding="utf-8"
                ) as f:
                    json.dump(
                        data, f, indent=2, ensure_ascii=False
                    )

                print(
                    f"  ✅ Raw BMS data saved to: {filename}"
                )

        else:
            print("  ❌ No BMS data received.")

        if not data:

            print(
                f"  ⚠️ No data for "
                f"{date_code or '(default)'}"
            )

            continue

        if movie_info["name"] == label:
            movie_info = parse_movie_info(data)

        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))

    if not all_shows:

        print("  ⚠️ No showtimes found.")

        # Don't destroy existing state on a temporary
        # BMS/API failure.
        return state, False, first_full_data

    print(
        f"  🎬 {movie_info['name']} "
        f"{movie_info['language']}"
    )

    filtered = filter_shows(
        all_shows,
        theatre,
        time_period,
        dates_filter,
    )

    print(
        f"  📊 {len(filtered)} "
        f"showtime(s) after filters"
    )

    new_watch_state = build_state(
        filtered,
        all_dates,
    )

    old_watch_state = state.get(label, {})

    changes = []

    if old_watch_state:
        changes = detect_changes(
            old_watch_state,
            new_watch_state,
        )

    state[label] = new_watch_state

    if changes:

        print(
            f"\n  ⚡ "
            f"{len(changes)} change(s) detected:"
        )

        for change in changes:
            print(f"     {change}")

        # send_email(
        #     label,
        #     (
        #         f"BMS Alert: "
        #         f"{movie_info['name']} - "
        #         f"{len(changes)} change(s)"
        #     ),
        #     changes,
        #     filtered,
        #     movie_info,
        # )

        send_telegram(
             label,
                        (
                            f"BMS Alert: "
                            f"{movie_info['name']} - "
                            f"{len(changes)} change(s)"
                        ),
                        changes,
                        filtered,
                        movie_info,
        )

    else:
        print("  ✅ No changes since last check.")

    print(
        f"\n  Current status "
        f"({len(filtered)} shows):"
    )

    for show in filtered:

        categories = ", ".join(
            (
                f"{category.name}"
                f"=₹{category.price}"
                f"({AVAIL_STATUS_MAP.get(
                    category.status,
                    ('?', '')
                )[0]})"
            )
            for category in show.categories
        )

        screen = (
            f"|{show.screen_attr}"
            if show.screen_attr
            else ""
        )

        print(
            f"    {show.venue_name} — "
            f"{show.time}{screen} "
            f"[{show.date_code}] — "
            f"{categories}"
        )

    return state, True, first_full_data


# ======================================================================
# RUN ONE WATCH
# ======================================================================
def run_watch(
    watch,
    state,
):

    watch_name = watch["name"]

    print("")
    print("=" * 70)
    print(
        f"🎬 WATCH: {watch_name}"
    )
    print("=" * 70)

    url = watch["url"]

    parsed = parse_bms_url(url)

    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get(
        "date_code",
        "",
    )

    if not event_code or not region_slug:

        print(
            "  ❌ Invalid BMS URL."
        )

        print(
            "     Could not extract "
            "event code or region."
        )

        return state, False

    (
        region_code,
        region_slug_resolved,
        lat,
        lon,
        geohash,
    ) = resolve_region(
        region_slug
    )

    # --------------------------------------------------------------
    # Dates
    # --------------------------------------------------------------

    configured_dates = watch.get(
        "dates",
        [],
    )

    if configured_dates:

        date_list = [
            str(d).strip()
            for d in configured_dates
            if str(d).strip()
        ]

    elif url_date:

        date_list = [url_date]

    else:

        date_list = [""]

    print(
        f"  Event: {event_code}"
    )

    print(
        f"  Region: {region_code}"
    )

    print(
        f"  Dates: {date_list}"
    )

    print(
        f"  Theatre: "
        f"{watch.get('theatre', []) or 'ALL'}"
    )

    print(
        f"  Time: "
        f"{watch.get('time_period', []) or 'ALL'}"
    )

    if watch.get("discover_variants"):

        print(
            f"  Language/format discovery: ON "
            f"(languages={watch.get('languages') or 'ANY'}, "
            f"formats={watch.get('formats') or 'ANY'})"
        )

    # --------------------------------------------------------------
    # Base event
    # --------------------------------------------------------------

    state, success, first_full_data = run_event(
        label=watch_name,
        event_code=event_code,
        region_code=region_code,
        region_slug_resolved=region_slug_resolved,
        lat=lat,
        lon=lon,
        geohash=geohash,
        date_list=date_list,
        theatre=watch.get("theatre", []),
        time_period=watch.get("time_period", []),
        dates_filter=watch.get("dates", []),
        state=state,
        save_raw_prefix=(
            f"bms_response_{watch_name}"
        ),
    )

    overall_success = success

    # Filter base event state if language doesn't match expected whitelist
    if watch.get("languages") and first_full_data:
        base_info = parse_movie_info(first_full_data)
        base_lang = base_info.get("language", "").lower()

        if not any(lang in base_lang for lang in watch["languages"]):
            print(
                f"  ⚠️ Skipping base watch state: language "
                f"('{base_info.get('language')}') not in allowed list {watch['languages']}"
            )
            state.pop(watch_name, None)

    # --------------------------------------------------------------
    # Language/format variant discovery
    # --------------------------------------------------------------

    if watch.get("discover_variants") and first_full_data:

        variants = parse_format_selector(first_full_data)

        if not variants:

            print(
                "  ℹ️ No language/format chips found "
                "for this event."
            )

        else:

            selected = select_variants(
                variants,
                watch.get("languages", []),
                watch.get("formats", []),
            )

            print(
                f"  🌐 Discovered {len(variants)} "
                f"language/format variant(s); "
                f"{len(selected)} selected to track "
                f"(besides the base watch)."
            )

            # Auto-update watch URL in watches.json maintaining full /movies/{region}/ path
            if len(selected) == 1 and selected[0].event_code:
             if not any(lang in base_lang for lang in watch["languages"]):
                target_variant = selected[0]
                v_code = target_variant.event_code
                clean_region = region_slug_resolved or region_slug or "chennai"

                # Extract title slug from chip path if present
                title_slug = ""
                if target_variant.event_url:
                    parts = [p for p in target_variant.event_url.strip("/").split("/") if p and p != v_code and p != "movies" and p != clean_region]
                    if parts:
                        title_slug = parts[-1]

                if not title_slug:
                    title_slug = f"movie-{v_code}"

                # Reconstruct standardized URL format: https://in.bookmyshow.com/movies/{region}/{title_slug}/{event_code}
                new_url = f"https://in.bookmyshow.com/movies/{clean_region}/{title_slug}/{v_code}"

                if watch["url"] != new_url:
                    print(
                        f"  💡 Auto-updating watch URL to target variant directly: {new_url}"
                    )
                    watch["url"] = new_url

                    try:
                        if os.path.exists(WATCHES_FILE):
                            with open(WATCHES_FILE, "r", encoding="utf-8") as f:
                                watches_data = json.load(f)

                            for w in watches_data:
                                if w.get("name") == watch_name:
                                    w["url"] = new_url

                            with open(WATCHES_FILE, "w", encoding="utf-8") as f:
                                json.dump(watches_data, f, indent=2, ensure_ascii=False)
                    except Exception as e:
                        print(f"  ⚠️ Could not update {WATCHES_FILE}: {e}")

            for variant in selected:

                variant_name = (
                    f"{watch_name} "
                    f"({variant.language} {variant.format})"
                )

                print(
                    f"\n  --- Variant: {variant_name} "
                    f"[{variant.event_code}] ---"
                )

                state, variant_success, _ = run_event(
                    label=variant_name,
                    event_code=variant.event_code,
                    region_code=region_code,
                    region_slug_resolved=region_slug_resolved,
                    lat=lat,
                    lon=lon,
                    geohash=geohash,
                    date_list=date_list,
                    theatre=watch.get("theatre", []),
                    time_period=watch.get("time_period", []),
                    dates_filter=watch.get("dates", []),
                    state=state,
                    save_raw_prefix=(
                        f"bms_response_{variant_name}"
                    ),
                )

                overall_success = (
                    overall_success or variant_success
                )

    return state, overall_success
# ======================================================================
# MAIN
# ======================================================================

def main():

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[{now}] "
        f"BMS Ticket Checker — CI mode"
    )

    watches = load_watches()

    print(
        f"📋 Loaded {len(watches)} watch(es)"
    )

    state = load_state()

    successful = 0

    for watch in watches:

        try:

            state, success = run_watch(
                watch,
                state,
            )

            if success:
                successful += 1

        except Exception as e:

            print("")
            print(
                f"❌ Watch "
                f"'{watch['name']}' "
                f"failed:"
            )

            print(
                f"   {type(e).__name__}: {e}"
            )

            # Continue with the remaining watches.
            continue

    # --------------------------------------------------------------
    # Save global state
    # --------------------------------------------------------------

    save_state(state)

    print("")
    print("=" * 70)

    print(
        f"✅ Completed: "
        f"{successful}/{len(watches)} watch(es)"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
