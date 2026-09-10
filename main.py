import os
import re
import sys
import json
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
from collections import defaultdict
from zoneinfo import ZoneInfo
from curl_cffi import requests
import html
import time
import random
# ======================================================================
# CONFIGURATION
# ======================================================================

WATCHES_FILE = "data/watches.json"
STATE_FILE = "data/bms_state.json"


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GROUP_CHAT_ID=os.getenv("GROUP_CHAT_ID", "").strip()

def get_notification_recipients() -> set[int]:
    raw_env = os.getenv("NOTIFICATION_USERS", "")
    recipients = {int(x.strip()) for x in raw_env.split(",") if x.strip().isdigit()}
    
    # Optional fallback for backward compatibility
    fallback_id = os.getenv("TELEGRAM_CHAT_ID")
    if not recipients and fallback_id and fallback_id.isdigit():
        recipients.add(int(fallback_id))
        
    return recipients

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
    "midnight": (0, 600),          # 12:00 AM - 05:59 AM (Special FDFS & Midnight shows)
    "morning": (600, 1200),    # 06:00 AM - 11:59 AM (Regular morning shows)
    "afternoon": (1200, 1600), # 12:00 PM - 03:59 PM (Matinee shows)
    "evening": (1600, 1900),   # 04:00 PM - 06:59 PM (Evening shows)
    "night": (1900, 2400),     # 07:00 PM - 11:59 PM (Night shows)
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


def cleanup_state(state):
    """
    Scans the bms_state.json dictionary and deletes any shows or tracked 
    dates that are older than today's date in IST.
    """
    today_int = int(datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d"))
    
    for watch_name, watch_data in list(state.items()):
        
        # 1. Clean up old shows
        shows = watch_data.get("shows", {})
        expired_show_keys = [
            key for key, show_info in shows.items()
            if show_info.get("date") and str(show_info.get("date")).isdigit() and int(show_info.get("date")) < today_int
        ]
        
        for key in expired_show_keys:
            del shows[key]
            
        # 2. Clean up old dates tracking
        dates = watch_data.get("dates", {})
        expired_date_keys = [
            date_code for date_code in dates.keys()
            if str(date_code).isdigit() and int(date_code) < today_int
        ]
        
        for date_code in expired_date_keys:
            del dates[date_code]
            
    return state


# ======================================================================
# WATCH CONFIGURATION
# ======================================================================

def format_date(date_str):
    """Converts '20260912' to '12/09/2026'."""
    try:
        return datetime.strptime(str(date_str), "%Y%m%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return date_str
    
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

        message_thread_id = watch.get("message_thread_id", None)

        validated.append({
            "name": name,
            "url": url,
            "dates": dates,
            "theatre": theatre,
            "time_period": time_period,
            "discover_variants": discover_variants,
            "languages": languages,
            "formats": formats,
            "message_thread_id": message_thread_id,  # <-- ADD THIS LINE
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
    max_retries=3,
):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://in.bookmyshow.com/movies/{region_slug}/buytickets/{event_code}/",
        "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
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

    # Introduce a polite, random delay before every request to avoid 403 blocks
    time.sleep(random.uniform(1.5, 3.0))

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                API_URL,
                headers=headers,
                params=params,
                impersonate="chrome",
                timeout=20,
            )

            if response.status_code == 200:
                return response.json()

            print(
                f"  ⚠️ BMS HTTP {response.status_code} (Attempt {attempt}/{max_retries})"
            )

            if response.status_code == 403:
                # Exponential backoff on 403 rate limits
                time.sleep(attempt * 3)

        except requests.RequestException as e:
            print(f"  ⚠️ BMS request failed: {e} (Attempt {attempt}/{max_retries})")
            time.sleep(2)

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

def detect_changes(old_state, new_state):
    changes = []

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    # 1. New showtimes added
    for key in set(new_shows) - set(old_shows):
        show = new_shows[key]
        changes.append({
            "type": "NEW",
            "icon": "🆕",
            "venue": show["venue"],
            "time": show["time"],
            "date": show["date"],
            "cat": show["cat"],
            "price": show["price"],
            "screen": show.get("screen", ""),
            "status": str(show.get("status", "3")),
            "old_status": ""
        })

    # Compare existing shows for status & price changes
    for key, new_show in new_shows.items():
        old_show = old_shows.get(key)
        if not old_show:
            continue

        old_status = str(old_show.get("status", "")).strip()
        new_status = str(new_show.get("status", "")).strip()

        # 2. Restocked check (0->1,2,3 | 1->2,3 | 2->3)
        if is_restocked(old_status, new_status):
            label, icon = AVAIL_STATUS_MAP.get(new_status, ("UNKNOWN", "🔄"))
            changes.append({
                "type": "RESTOCKED",
                "icon": icon,
                "venue": new_show["venue"],
                "time": new_show["time"],
                "date": new_show["date"],
                "cat": new_show["cat"],
                "price": new_show["price"],
                "old_price": old_show.get("price"),
                "screen": new_show.get("screen", ""),
                "status": new_status,        # Pass raw key e.g. "3"
                "old_status": old_status     # Pass raw key e.g. "0" or "1"
            })

        # 3. Price changes
        try:
            old_price = float(old_show.get("price", 0))
            new_price = float(new_show.get("price", 0))

            if old_price != new_price:
                price_dropped = new_price < old_price
                changes.append({
                    "type": "PRICE_DROP" if price_dropped else "PRICE_INCREASE",
                    "icon": "📉" if price_dropped else "📈",
                    "venue": new_show["venue"],
                    "time": new_show["time"],
                    "date": new_show["date"],
                    "cat": new_show["cat"],
                    "old_price": f"{old_price:.2f}",
                    "price": f"{new_price:.2f}",
                    "screen": new_show.get("screen", ""),
                    "status": new_status,
                    "old_status": old_status
                })
        except (ValueError, TypeError):
            pass

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
# 2. HELPER FUNCTIONS
def resolve_status_info(status_key):
    """
    Translates raw status keys ('0', '1', '2', '3') into tuple (Label, Emoji).
    """
    key = str(status_key).strip()
    if key in AVAIL_STATUS_MAP:
        return AVAIL_STATUS_MAP[key]
    return (key if key else "UNKNOWN", "⚪")

def is_restocked(old_status_key, new_status_key):
    """
    Evaluates if state transition represents restocking (e.g., 0 -> 1, 2, 3 or 1 -> 2, 3).
    """
    try:
        old_lvl = int(str(old_status_key).strip())
        new_lvl = int(str(new_status_key).strip())
        return new_lvl > old_lvl
    except (ValueError, TypeError):
        return False

def parse_time_to_minutes(time_str):
    try:
        t_str = str(time_str).strip().upper()
        if "AM" in t_str or "PM" in t_str:
            dt = datetime.strptime(t_str, "%I:%M %p")
        else:
            dt = datetime.strptime(t_str, "%H:%M")
        return dt.hour * 60 + dt.minute
    except Exception:
        return 0

def parse_price(price_val):
    try:
        cleaned = ''.join(c for c in str(price_val) if c.isdigit() or c == '.')
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0

def format_date(date_str):
    try:
        dt = datetime.strptime(str(date_str), "%Y%m%d")
        return dt.strftime("%a, %d %b %Y")
    except Exception:
        return str(date_str)

def split_message_chunks(lines, max_chars=4000):
    chunks = []
    current_chunk = []
    current_length = 0

    for line in lines:
        line_len = len(line) + 1
        if current_length + line_len > max_chars:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_length = line_len
        else:
            current_chunk.append(line)
            current_length += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks

# 4. TELEGRAM ALERT DISPATCHER
def send_telegram(threadid, watch_name, subject, changes, shows, movie_info):
    # Ensure both variables are available (assuming they are loaded from your .env globally)
    # GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
    # TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    
    if not TELEGRAM_BOT_TOKEN or not GROUP_CHAT_ID:
        print(" ⚠️ Telegram skipped — TELEGRAM_BOT_TOKEN or GROUP_CHAT_ID not configured.")
        return

    if not changes:
        return

    now_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %I:%M %p")
    movie_name = movie_info.get("name", watch_name)

    def get_type_priority(change_type):
        priority_map = {
            "NEW": 1,
            "RESTOCKED": 2,
            "PRICE_DROP": 3,
            "PRICE_INCREASE": 4
        }
        return priority_map.get(change_type, 99)

    # 1. SORT
    sorted_changes = sorted(
        changes,
        key=lambda x: (
            str(x.get("date", "")),
            get_type_priority(x.get("type")),
            str(x.get("venue", "")).lower(),
            parse_time_to_minutes(x.get("time", "")),
            parse_price(x.get("price", 0))
        )
    )

    # 2. GROUP
    nested_data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for item in sorted_changes:
        d_val = item["date"]
        c_type = item["type"]
        show_key = (
            item["venue"],
            item["time"],
            item.get("screen", ""),
            item.get("icon", "🔄")
        )
        nested_data[d_val][c_type][show_key].append(item)

    # 3. BUILD LINES
    lines = [
        f"🚨 <b>BMS Ticket Alert!</b>",
        f"🎬 <b>{html.escape(str(watch_name.split('_')[0]))}</b> ({html.escape(str(watch_name))})",
        f"🕒 <i>{html.escape(now_str)}</i>\n",
    ]

    section_headers = {
        "NEW": "==========================\n🆕 <b>NEW SHOW ADDED</b>\n==========================",
        "RESTOCKED": "==========================\n🔄 <b>TICKETS STATUS CHANGE</b>\n==========================",
        "PRICE_DROP": "==============================\n📉 <b>PRICE DROP ALERT</b>\n==============================",
        "PRICE_INCREASE": "============================\n📈 <b>PRICE INCREASE ALERT</b>\n============================"
    }

    # 4. RENDER
    for date_val, type_groups in nested_data.items():
        formatted_date = format_date(date_val)
        lines.append(
            f"░░░░░░░░░░░░░░░░░░░\n"
            f"📆 <b>SHOWS FOR: {html.escape(str(formatted_date)).upper()}</b>\n"
            f"░░░░░░░░░░░░░░░░░░░\n"
        )

        for c_type in sorted(type_groups.keys(), key=get_type_priority):
            shows_dict = type_groups[c_type]
            lines.append(section_headers.get(c_type, f"<b>{c_type}</b>"))

            for (venue, time_val, screen, icon), items in shows_dict.items():
                screen_str = f" [{html.escape(str(screen))}]" if screen else ""
                
                cat_lines = []
                for cat in items:
                    cat_name = html.escape(str(cat.get('cat', '')))
                    cat_price = html.escape(str(cat.get('price', '')))
                    old_price = html.escape(str(cat.get('old_price', '')))
                    
                    raw_status = str(cat.get('status', '3')).strip()
                    raw_old_status = str(cat.get('old_status', '')).strip() if cat.get('old_status') is not None else ""
                    
                    if c_type == "RESTOCKED" and not raw_old_status:
                        raw_old_status = "0"

                    curr_label, curr_emoji = resolve_status_info(raw_status)
                    old_label, old_emoji = resolve_status_info(raw_old_status) if raw_old_status != "" else ("", "")

                    if c_type == "RESTOCKED":
                        if old_label and old_label != curr_label:
                            status_str = f"{old_emoji} {old_label} → {curr_emoji} <b>{curr_label}</b>"
                        else:
                            status_str = f"{curr_emoji} <b>{curr_label}</b>"
                        cat_lines.append(f"└ 🎟️ {cat_name}: ₹{cat_price} ({status_str})")
                    elif c_type in ("PRICE_DROP", "PRICE_INCREASE"):
                        cat_lines.append(f"└ 🎟️ {cat_name}: Was ₹{old_price} ➔ <b>₹{cat_price}</b> ({curr_emoji} {curr_label})")
                    else: # NEW or DEFAULT
                        cat_lines.append(f"└ 🎟️ {cat_name}: ₹{cat_price} ({curr_emoji} {curr_label})")

                categories_formatted = "\n".join(cat_lines)

                lines.append(
                    f"📍 {html.escape(str(venue))}\n"
                    f"🕒 <code>{html.escape(str(time_val))}</code>{screen_str}\n"
                    f"{categories_formatted}\n"
                )

    # 5. DISPATCH CHUNKS TO THE FORUM TOPIC IN THE GROUP
    message_chunks = split_message_chunks(lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    alert_failed = False
    last_error = "Unknown Error"

    for chunk in message_chunks:
        chunk_success = False
        for attempt in range(1, 4):
            try:
                payload = {
                    "chat_id": GROUP_CHAT_ID, # Main Group ID
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": False, # Explicitly enable sound/push notifications
                }
                
                # Direct it to the specific topic thread
                if threadid:
                    payload["message_thread_id"] = threadid

                response = requests.post(url, json=payload, timeout=20)
                
                if response.status_code == 200:
                    chunk_success = True
                    break
                else:
                    last_error = f"HTTP {response.status_code}: {response.text}"
            except requests.RequestException as e:
                last_error = str(e)

            time.sleep(attempt * 2)

        if not chunk_success:
            alert_failed = True
            break

    # 6. FALLBACK: SEND FAILURE REPORT DIRECTLY TO YOU (ADMIN)
    if alert_failed and TELEGRAM_CHAT_ID:
        report_text = (
            f"⚠️ <b>Delivery Failure Report</b>\n"
            f"Failed to deliver alert for <b>{html.escape(str(movie_name))}</b> to Topic ID <code>{threadid}</code> in the Group.\n"
            f"❌ <b>Reason:</b> <code>{html.escape(last_error)}</code>\n\n"
            f"<i>(Note: This usually happens if the topic was deleted or the bot lacks permissions.)</i>"
        )
        try:
            # Send directly to your admin DM (TELEGRAM_CHAT_ID)
            requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": report_text,
                    "parse_mode": "HTML"
                },
                timeout=10,
            )
            # Next, send the actual message chunks that failed to deliver to the group
            for chunk in message_chunks:
                requests.post(
                    url,
                    json={
                        "chat_id": TELEGRAM_CHAT_ID, # Sending directly to Admin DM
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10,
                )
                time.sleep(1) # Brief pause between chunks to respect Telegram rate limits
        except Exception:
            pass

def get_telegram_user_info(chat_id: int) -> str:
    """Helper to fetch a user's name/username via getChat endpoint."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat"
        res = requests.post(url, json={"chat_id": chat_id}, timeout=5).json()
        if res.get("ok"):
            chat = res.get("result", {})
            first_name = chat.get("first_name", "")
            last_name = chat.get("last_name", "")
            full_name = f"{first_name} {last_name}".strip() or "Unknown"
            username = f" (@{chat['username']})" if chat.get("username") else ""
            return f"{full_name}{username}"
    except Exception:
        pass
    return "Unknown User"


# ======================================================================
# RUN A SINGLE EVENT (one language/format variant, or the base watch)
# ======================================================================

def run_event(
    threadid,
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


    # Filter out past dates before hitting the API
    today_int = int(datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d"))
    valid_dates = []

    for d in date_list:
        if not d: # Keep default empty date behavior
            valid_dates.append(d)
        elif str(d).isdigit() and int(d) >= today_int:
            valid_dates.append(d)
        else:
            print(f"  ⏭️ Skipping past date: {d}")

    for date_code in valid_dates:

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
            threadid,
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
    watch_threadid=watch["message_thread_id"]

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
        threadid=watch_threadid,
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
                            threadid=watch_threadid,
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
    state = cleanup_state(state)

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
