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
NTFY_URL=os.getenv("NTFY_URL","").strip()
NTFY_TOPIC=os.getenv("NTFY_TOPIC", "").strip()
NTFY_ERROR_TOPIC = os.getenv("NTFY_ERROR_TOPIC", "").strip()
DISCORD_WEBHOOK_URL=os.getenv("DISCORD_WEBHOOK_URL", "").strip()

def save_watches(watches):
    """Saves updated watches data back to the JSON file."""
    with open(WATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(watches, f, indent=2, ensure_ascii=False)

def get_notification_recipients() -> set[int]:
    raw_env = os.getenv("NOTIFICATION_USERS", "")
    recipients = {int(x.strip()) for x in raw_env.split(",") if x.strip().isdigit()}
    
    # Optional fallback for backward compatibility
    fallback_id = os.getenv("TELEGRAM_CHAT_ID")
    if not recipients and fallback_id and fallback_id.isdigit():
        recipients.add(int(fallback_id))
        
    return recipients

def send_watch_expiry_alert(watch, idx):
    if not TELEGRAM_BOT_TOKEN or not GROUP_CHAT_ID:
        return

    threadid = watch.get("message_thread_id")
    watch_name = watch.get("name", f"Watch_{idx}")
    
    # 🔥 Extract the UID to match the bot's new system
    timestamp_match = re.search(r'_(\d{10,})', watch_name)
    uid = timestamp_match.group(1) if timestamp_match else watch_name.split('_')[0][:20]
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # 🔥 Use the UID in the callback_data
    kb = {
        "inline_keyboard": [
            [{"text": "Close Topic", "callback_data": f"confirmstop_{uid}"}],
            [{"text": "Delete Topic", "callback_data": f"confirmstopperm_{uid}"}]
        ]
    }
    
    text = (
        f"⏰ <b>Tracker Expired!</b>\n\n"
        f"All the configured dates for <code>{html.escape(watch_name)}</code> are now in the past.\n"
        f"So now the tracker is stopped."
    )
    
    payload = {
        "chat_id": GROUP_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": kb
    }
    if threadid:
        payload["message_thread_id"] = threadid
        
    try:
      response = requests.post(url, json=payload, timeout=10)

      if response.status_code == 200:
        print(f"  🔔 Expiry notification sent for {watch_name}")
      else:
        print(
            f"  ⚠️ Failed to send expiry notification "
            f"for {watch_name}: HTTP {response.status_code} {response.text}"
        )

    except Exception as e:
      print(f"  ⚠️ Failed to send expiry notification: {e}")

def delete_discord_forum_thread(thread_id: str):
    """Permanently deletes a Discord Forum thread using the Bot API."""
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not bot_token or not thread_id:
        return

    # Discord API endpoint to delete a channel/thread
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    headers = {
        "Authorization": f"Bot {bot_token}"
    }

    try:
        res = requests.delete(url, headers=headers, timeout=10)
        if res.status_code in [200, 204]:
            print(f"✅ Successfully deleted Discord thread ID: {thread_id}")
        else:
            print(f"⚠️ Failed to delete Discord thread {thread_id}: HTTP {res.status_code} {res.text}")
    except Exception as e:
        print(f"⚠ Discord API Error deleting thread: {e}")

# ======================================================================
# CONSTANTS
# ======================================================================

# ======================================================================
# CINEMA CHAIN SESSION URL MAPPING (VCODE MATCHING)
# ======================================================================

CINEMA_CHAIN_URLS = {
    # INOX
    "INTO": "https://www.inoxmovies.com/cinemasessions/Chennai/INOX-The-Marina-Mall,-OMR,-Chennai/232",
    "INPR": "https://www.inoxmovies.com/cinemasessions/Chennai/INOX-Luxe-Phoenix-Market-City,-Velachery--(formerly-Jazz-Cinemas)Chennai/320",
    "INCH": "https://www.inoxmovies.com/cinemasessions/Chennai/INOX-Chennai-Citi-Centre,Dr.-R.-K.-Salai-Chennai/113",
    "FMCN": "https://www.inoxmovies.com/cinemasessions/Chennai/INOX-National,Virugambakkam-Chennai/28",

    # PVR
    "PVHR": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-Heritage-RSL-ECR-Chennai/417",
    "PGMV": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR,-Grand-Mall,-Velachery/389",
    "PVES": "https://www.pvrcinemas.com/cinemasessions/Chennai/HDFC-Millennia-PVR:-Escape-Express-Avenue-Mall/359",
    "PVSR": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-Sathyam-Royapettah-Chennai/331",
    "PABC": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-AEROHUB-Chennai/432",
    "PGRA": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-Grand-Galada-Chennai/400",
    "PVPZ": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-Palazzo-The-Nexus-Vijaya-Mall/388",
    "PVHC": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR,-Ampa-Mall,-Nelson-Manickam-Road-Chennai/358",
    "PCAN": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-VR-Chennai-Anna-Nagar/523",
    "PBRM": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-Perambur---Spectrum-Mall-Chennai/372",
    "PSKL": "https://www.pvrcinemas.com/cinemasessions/Chennai/PVR-SKLS-Galaxy-Mall,-Red-Hills-Chennai/410",

    #Cinepolis
    "CBMC":"https://cinepolisindia.com/movie-list/38"
}


AVAIL_STATUS_MAP = {
    "0": ("SOLD", "🔴"),
    "1": ("FEW", "🟠"),
    "2": ("FAST", "🟡"),
    "3": ("AVL", "🟢"),
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

def is_show_in_future(date_code, time_str):
    """Checks if a given BMS date_code and time string are in the future."""
    if not date_code or not time_str:
        return True
    try:
        # Clean hidden spaces BMS sometimes uses
        clean_time = str(time_str).strip().upper().replace("\xa0", " ")
        clean_time = " ".join(clean_time.split()) 
        
        dt_str = f"{str(date_code).strip()} {clean_time}"
        fmt = "%Y%m%d %I:%M %p" if "AM" in clean_time or "PM" in clean_time else "%Y%m%d %H:%M"
        show_dt = datetime.strptime(dt_str, fmt).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return show_dt > datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return True # Fallback safely if format behaves unexpectedly
     
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
def send_ntfy_error(movie_name, date_code):
    if not NTFY_ERROR_TOPIC:
        return
    
    url = f"{NTFY_URL}/{NTFY_ERROR_TOPIC}"
    headers = {
        "Title": "⚠️ Shows Fetch Failed",
        "Priority": "high",
        "Tags": "warning,rotating_light"
    }
    
    date_str = date_code if date_code else "default date"
    message = f"Failed to fetch showtimes for '{movie_name}' (Date: {date_str}). Cloudflare or rate-limit block detected."
    
    try:
        requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        print(f"  ⚠️ Ntfy error alert failed: {e}")


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
    Load watches from watches.json and preserve retention/status fields.
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

        # --- ADVANCED DATE & TIME PARSING ---
        dates_raw = watch.get("dates", [])
        time_period_global = [tp.lower() for tp in _as_list(watch.get("time_period"))]
        date_time_map = {}
        
        if isinstance(dates_raw, dict):
            for d_key, d_times in dates_raw.items():
                clean_d = str(d_key).strip()
                if clean_d:
                    date_time_map[clean_d] = [t.lower() for t in _as_list(d_times)]
            dates_list = list(date_time_map.keys())
        else:
            if isinstance(dates_raw, str):
                dates_list = [d.strip() for d in dates_raw.split(",") if d.strip()]
            elif isinstance(dates_raw, list):
                dates_list = [str(d).strip() for d in dates_raw if str(d).strip()]
            else:
                dates_list = []

        theatre = [t.lower() for t in _as_list(watch.get("theatre"))]
        discover_variants = bool(watch.get("discover_variants", False))
        languages = [lang.lower() for lang in _as_list(watch.get("languages"))]
        formats = [fmt.lower() for fmt in _as_list(watch.get("formats"))]
        message_thread_id = watch.get("message_thread_id", None)
        discord_thread_id = watch.get("discord_thread_id", None)


        # --- PRESERVE RETENTION & STATUS FIELDS ---
        status = watch.get("status", None)
        closed_at = watch.get("closed_at", None)
        closed_at_formatted = watch.get("closed_at_formatted", None)
        deletes_at_formatted = watch.get("deletes_at_formatted", None)
        expired_notified = watch.get("expired_notified", False)

        validated_watch = {
            "name": name,
            "url": url,
            "dates": dates_list,
            "date_time_map": date_time_map,
            "theatre": theatre,
            "time_period": time_period_global,
            "discover_variants": discover_variants,
            "languages": languages,
            "formats": formats,
            "message_thread_id": message_thread_id,
            "expired_notified": expired_notified,
            "discord_thread_id":discord_thread_id
        }

        # Keep status fields if they exist
        if status:
            validated_watch["status"] = status
        if closed_at:
            validated_watch["closed_at"] = closed_at
        if closed_at_formatted:
            validated_watch["closed_at_formatted"] = closed_at_formatted
        if deletes_at_formatted:
            validated_watch["deletes_at_formatted"] = deletes_at_formatted

        validated.append(validated_watch)

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
    show_name,  # <-- ADDED THIS PARAMETER
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
                
            # If the API explicitly says Not Found/Bad Request, it just hasn't opened yet.
            # Quit quietly without alerting.
            if response.status_code in [400, 404]:
                print(f"  ℹ️ BMS HTTP {response.status_code}: Shows likely not opened for {date_code}.")
                return None

            print(
                f"  ⚠️ BMS HTTP {response.status_code} (Attempt {attempt}/{max_retries})"
            )

            if response.status_code in [403, 429]:
                # Exponential backoff on rate limits/blocks
                time.sleep(attempt * 3)
            else:
                time.sleep(2)

        except requests.RequestException as e:
            print(f"  ⚠️ BMS request failed: {e} (Attempt {attempt}/{max_retries})")
            time.sleep(2)

    # --- IF IT REACHES HERE, ALL RETRIES FAILED ---
    print(f"  ❌ Failed to fetch BMS API after {max_retries} attempts.")
    send_ntfy_error(show_name, date_code)
    
    return None
# ======================================================================
# MOVIE INFO PARSER
# ======================================================================

# ======================================================================
# MOVIE INFO PARSER
# ======================================================================
def parse_movie_info(data, fallback_name="Unknown Movie"):
    info = {
        "name": fallback_name,
        "language": "",
    }

    # 1. Grab Language and Format
    for widget in data.get("data", {}).get("topStickyWidgets", []):
        if widget.get("type") == "horizontal-text-list":
            for item in widget.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for component in row.get("components", []):
                        text = component.get("text", "")
                        if "•" in text:
                            info["language"] = text.strip()

    # 2. Hunt down the True Movie Name (Checking 3 different BMS locations!)
    meta_name = data.get("meta", {}).get("eventName")
    banner_name = data.get("data", {}).get("banner", {}).get("name")
    bottom_name = None
    
    bottom_sheet = data.get("data", {}).get("bottomSheetData", {})
    for widget in bottom_sheet.get("format-selector", {}).get("widgets", []):
        if widget.get("type") == "vertical-text-list":
            for item in widget.get("data", []):
                # Check both subtitle and title just in case BMS changes the style ID
                if item.get("styleId") in ["bottomsheet-subtitle", "bottomsheet-title"]:
                    bottom_name = item.get("text")
                    break

    # Pick the most accurate name available
    if meta_name:
        info["name"] = meta_name
    elif bottom_name:
        info["name"] = bottom_name
    elif banner_name:
        info["name"] = banner_name

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
# ======================================================================
# FILTERING
# ======================================================================

def filter_shows(
    shows,
    theatre_filter,
    time_periods_global,
    date_codes,
    date_time_map=None, # <--- Added parameter
):
    if date_time_map is None:
        date_time_map = {}

    result = []
    theatre_keywords = theatre_filter if theatre_filter else []
    dates_set = (
        set(d.strip() for d in date_codes if str(d).strip())
        if date_codes else set()
    )

    for show in shows:
        if not is_show_in_future(show.date_code, show.time):
            continue
        # 1. Theatre filter
        if theatre_keywords:
            venue_lower = show.venue_name.lower()
            if not any(keyword in venue_lower for keyword in theatre_keywords):
                continue

        # 2. Date filter
        if dates_set and show.date_code and show.date_code not in dates_set:
            continue

        # 3. Time filter (Check map specific to this date first, then global fallback)
        if show.date_code in date_time_map:
            periods = date_time_map[show.date_code]
        else:
            periods = time_periods_global

        if periods:
            try:
                time_code = int(show.time_code)
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
                "vcode": show.venue_code,  # <--- ADD THIS
            "sid": show.session_id,    # <--- ADD THIS
            "missing_count": 0,  # <-- NEW: Freshly seen shows have 0 missing count
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
            "old_status": "",
            "vcode": show.get("vcode", ""),
            "sid": show.get("sid", "")
        })

    # Compare existing shows for status & price changes
    for key, new_show in new_shows.items():
        old_show = old_shows.get(key)
        if not old_show:
            continue

        old_status = str(old_show.get("status", "")).strip()
        new_status = str(new_show.get("status", "")).strip()

        # 2. Restocked check
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
                "status": new_status,
                "old_status": old_status,
                "vcode": new_show.get("vcode", ""), # Fixed variable
                "sid": new_show.get("sid", "")      # Fixed variable
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
                    "old_status": old_status,
                    "vcode": new_show.get("vcode", ""), # Fixed variable
                    "sid": new_show.get("sid", "")      # Fixed variable
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
    return (key if key else "NA", "⚪")

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

def send_ntfy(label, movie_info, changes):
    if not NTFY_TOPIC or not changes:
        return

    movie_name = movie_info.get("name", label)
    
    # Group by date -> type -> unique venues/times
    date_groups = defaultdict(lambda: defaultdict(set))
    for change in changes:
        date_val = change.get("date", "")
        c_type = change.get("type", "UPDATE")
        venue = change.get("venue", "Unknown").strip()
        time_val = change.get("time", "").strip()
        
        date_groups[date_val][c_type].add(f"{venue} ({time_val})")
        
    url = f"{NTFY_URL}/{NTFY_TOPIC}"

    for date_val in sorted(date_groups.keys()):
        types_dict = date_groups[date_val]
        formatted_date = format_date(date_val)
        
        summary_parts = []
        if "NEW" in types_dict:
            shows_str = ", ".join(sorted(types_dict["NEW"]))
            summary_parts.append(f"New show added in {shows_str}")
            
        if "RESTOCKED" in types_dict:
            shows_str = ", ".join(sorted(types_dict["RESTOCKED"]))
            summary_parts.append(f"Ticket status changed in {shows_str}")
            
        price_changes = types_dict.get("PRICE_DROP", set()).union(types_dict.get("PRICE_INCREASE", set()))
        if price_changes:
            shows_str = ", ".join(sorted(price_changes))
            summary_parts.append(f"Price changed in {shows_str}")
            
        if not summary_parts:
            continue
            
        # Keep fun tags, but force everything to High Priority
        if "NEW" in types_dict:
            tags = "rotating_light,fire"
        elif "RESTOCKED" in types_dict:
            tags = "bell,popcorn"
        else:
            tags = "ticket,money_with_wings"

        headers = {
            "Title": f"Show Alert: {movie_name} - {formatted_date}",  # <-- Date added to title
            "Priority": "high",  # Hardcoded to high for ALL alerts
            "Tags": tags,
        }

        # Removed the date from the beginning of the message text to avoid redundancy
        message_text = f"{'; '.join(summary_parts)}."
        
        try:
            response = requests.post(
                url, 
                data=message_text.encode("utf-8"), 
                headers=headers, 
                timeout=10
            )
            if response.status_code != 200:
                print(f"  ⚠️ Ntfy failed: HTTP {response.status_code}")
        except Exception as e:
            print(f"  ⚠️ Ntfy error: {e}")

        time.sleep(0.5)


        # 4. TELEGRAM ALERT DISPATCHER
# 4. TELEGRAM ALERT DISPATCHER
# 4. TELEGRAM ALERT DISPATCHER (SIMPLIFIED BLOCKS)
# ======================================================================
# Telegram
# ======================================================================
# ======================================================================
# Telegram
# ======================================================================

# ======================================================================
# REMOVED SHOW ALERT
# ======================================================================
# ======================================================================
# REMOVED SHOW TELEGRAM ALERT
# ======================================================================
def send_removed_shows_telegram(
    threadid,
    watch_name,
    removed_shows,
    movie_info,
):
    if not TELEGRAM_BOT_TOKEN or not GROUP_CHAT_ID:
        print(
            "  ⚠️ Removed-show Telegram skipped — "
            "TELEGRAM_BOT_TOKEN or GROUP_CHAT_ID not configured."
        )
        return

    if not removed_shows:
        return

    now_str = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%d %b, %I:%M %p")

    movie_name = movie_info.get(
        "name",
        watch_name.split("_")[0]
    )

    # --------------------------------------------------------------
    # Language / Format — same style as send_telegram()
    # --------------------------------------------------------------
    lang_fmt_match = re.search(
        r'\(([^)]+)\)$',
        str(watch_name)
    )

    lang_fmt_str = (
        f" 🗣️ <b>{html.escape(lang_fmt_match.group(1))}</b>"
        if lang_fmt_match
        else ""
    )

    # --------------------------------------------------------------
    # Sort:
    # DATE -> VENUE -> TIME
    # --------------------------------------------------------------
    sorted_shows = sorted(
        removed_shows,
        key=lambda x: (
            str(x.get("date", "")),
            str(x.get("venue", "")).lower(),
            parse_time_to_minutes(
                x.get("time", "")
            ),
            str(x.get("screen", "")).lower(),
        )
    )

    # --------------------------------------------------------------
    # Group:
    # DATE -> VENUE
    # --------------------------------------------------------------
    grouped = defaultdict(
        lambda: defaultdict(list)
    )

    for show in sorted_shows:
        grouped[
            str(show.get("date", ""))
        ][
            str(show.get("venue", ""))
        ].append(show)

    # --------------------------------------------------------------
    # HEADER
    # --------------------------------------------------------------
    lines = [
        "🚨 <b>SHOW REMOVED!</b>",
        f"🎬 <b>{html.escape(str(movie_name))}</b>{lang_fmt_str}",
        "🏷 <b>#SHOWREMOVED</b>",
        f"🕒 <i>{html.escape(now_str)}</i>",
        "",
    ]

    # --------------------------------------------------------------
    # DATE -> VENUE -> TIME
    # --------------------------------------------------------------
    for date_val, venues_dict in grouped.items():

        formatted_date = format_date(date_val)

        lines.append(
            "╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌"
        )

        lines.append(
            f"📅 <b>{html.escape(str(formatted_date)).upper()}</b>"
        )

        lines.append(
            "╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌"
        )

        # Venue alphabetical order
        for venue, venue_shows in sorted(
            venues_dict.items(),
            key=lambda x: x[0].lower()
        ):

            lines.append(
                f"🏢 <b>{html.escape(str(venue))}</b>"
            )

            # Time order inside venue
            venue_shows = sorted(
                venue_shows,
                key=lambda x: (
                    parse_time_to_minutes(
                        x.get("time", "")
                    ),
                    str(x.get("screen", "")).lower(),
                )
            )

            for show in venue_shows:

                time_val = str(
                    show.get("time", "")
                ).strip()

                screen = str(
                    show.get("screen", "")
                ).strip()

                vcode = str(
                    show.get("vcode", "")
                ).strip()

                sid = str(
                    show.get("sid", "")
                ).strip()

                # --------------------------------------------------
                # Screen formatting — SAME STYLE as send_telegram()
                # --------------------------------------------------
                raw_screen = (
                    screen if screen else "NORM"
                )

                if len(raw_screen) > 15:
                    raw_screen = (
                        raw_screen[:12] + "..."
                    )

                screen_name = html.escape(
                    raw_screen
                )

                chain_url = CINEMA_CHAIN_URLS.get(
                    vcode
                )

                if chain_url:
                    screen_str = (
                        f' [<a href="{chain_url}">'
                        f"{screen_name}"
                        f"</a>]"
                    )

                else:
                    venue_upper = venue.upper()

                    if "PVR" in venue_upper:
                        screen_str = (
                            f' [<a href="'
                            f'https://www.pvrcinemas.com/'
                            f'">{screen_name}</a>]'
                        )

                    elif "INOX" in venue_upper:
                        screen_str = (
                            f' [<a href="'
                            f'https://www.inoxmovies.com/'
                            f'">{screen_name}</a>]'
                        )

                    else:
                        screen_str = (
                            f" [{screen_name}]"
                        )

                # --------------------------------------------------
                # Direct BMS show link — TIME is clickable
                # --------------------------------------------------
                time_display = html.escape(
                    time_val
                )

                if vcode and sid:

                    book_url = (
                        "https://in.bookmyshow.com/"
                        f"booktickets/{vcode}/{sid}"
                    )

                    time_display = (
                        f'<a href="{book_url}">'
                        f"<b>{time_display}</b>"
                        f"</a>"
                    )

                else:
                    time_display = (
                        f"<b>{time_display}</b>"
                    )

                # --------------------------------------------------
                # Removed-show line
                # No price/status/category information here.
                # --------------------------------------------------
                lines.append(
                    f"❌{time_display}"
                    f"{screen_str} "
                )

            lines.append("")

        lines.append("")

    # --------------------------------------------------------------
    # DISPATCH — same retry/chunk style as send_telegram()
    # --------------------------------------------------------------
    message_chunks = split_message_chunks(
        lines
    )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    alert_failed = False
    last_error = "Unknown Error"

    for chunk in message_chunks:

        chunk_success = False

        for attempt in range(1, 4):

            try:

                payload = {
                    "chat_id": GROUP_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": False,
                }

                # IMPORTANT:
                # Send to the SAME forum topic as normal alerts.
                if threadid:
                    payload[
                        "message_thread_id"
                    ] = threadid

                response = requests.post(
                    url,
                    json=payload,
                    timeout=20,
                )

                if response.status_code == 200:
                    chunk_success = True
                    break

                last_error = (
                    f"HTTP {response.status_code}: "
                    f"{response.text}"
                )

            except requests.RequestException as e:
                last_error = str(e)

            time.sleep(attempt * 2)

        if not chunk_success:
            alert_failed = True
            break

    # --------------------------------------------------------------
    # ADMIN FALLBACK — same as send_telegram()
    # --------------------------------------------------------------
    if alert_failed:

        print(
            f"  ⚠️ Failed to send removed-show alert: "
            f"{last_error}"
        )

        if TELEGRAM_CHAT_ID:

            report_text = (
                "⚠️ <b>Delivery Failure Report</b>\n"
                f"Failed to deliver removed-show alert "
                f"for <b>{html.escape(str(movie_name))}</b> "
                f"to Topic ID <code>{threadid}</code> "
                "in the Group.\n"
                f"❌ <b>Reason:</b> "
                f"<code>{html.escape(last_error)}</code>"
            )

            try:

                requests.post(
                    url,
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": report_text,
                        "parse_mode": "HTML",
                    },
                    timeout=10,
                )

                for chunk in message_chunks:

                    requests.post(
                        url,
                        json={
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": chunk,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                        timeout=10,
                    )

                    time.sleep(1)

            except Exception:
                pass

    else:

        print(
            f"  🗑️ Removed-show alert sent "
            f"to thread {threadid} "
            f"({len(removed_shows)} show(s))"
        )
def update_discord_dashboard(threadid, watch_name, filtered_shows, movie_info, state):
    if not DISCORD_WEBHOOK_URL or not filtered_shows:
        return

    # Extract base movie name and variant tag (e.g., "Dorothy [Tamil 2D]")
    movie_name = movie_info.get("name", watch_name.split('_')[0])
    lang_fmt_match = re.search(r'\(([^)]+)\)$', str(watch_name))
    if lang_fmt_match:
        movie_name += f" [{lang_fmt_match.group(1)}]"
    
    # 1. Group ALL current shows by Date, then by Venue
    nested_data = defaultdict(lambda: defaultdict(list))
    for show in filtered_shows:
        nested_data[show.date_code][show.venue_name].append(show)

    all_embeds = []
    
    # 2. Build Embeds grouped by Date and Venue
    for date_code, venues_dict in sorted(nested_data.items()):
        formatted_date = format_date(date_code) # Formats 20261005 to readable date
        
        for venue, shows in sorted(venues_dict.items(), key=lambda x: x[0].lower()):
            for i in range(0, len(shows), 25):
                chunked_shows = shows[i:i+25]
                fields = []
                
                for show in chunked_shows:
                    if not show.categories: 
                        continue
                    
                    time_val = show.time
                    screen = show.screen_attr or "NORM"
                    
                    # Direct Deep Link
                    book_url = f"https://in.bookmyshow.com/booktickets/{show.venue_code}/{show.session_id}" if show.venue_code and show.session_id else "https://in.bookmyshow.com"
                    
                    # Render ALL categories for this showtime (Sorted High to Low Price)
                    sorted_cats = sorted(
                        show.categories,
                        key=lambda c: (-parse_price(c.price), str(c.name).lower())
                    )
                    
                    cats_parts = []
                    for cat in sorted_cats:
                        status_label, status_emoji = resolve_status_info(cat.status)
                        clean_price = f"₹{int(round(float(cat.price)))}" if str(cat.price).replace('.', '', 1).isdigit() else f"₹{cat.price}"
                        cats_parts.append(f"• **{cat.name}**: {clean_price} `{status_label} {status_emoji}`")
                        
                    cats_str = "\n".join(cats_parts)

                    fields.append({
                        "name": f"🕒 {time_val} ({screen[:12]})",
                        "value": f"[Book Tickets]({book_url})\n{cats_str}",
                        "inline": False # False allows vertical space for all price tiers
                    })

                title = f"📅 {formatted_date} | 🏢 {venue}" if i == 0 else f"📅 {formatted_date} | 🏢 {venue} (Cont.)"
                all_embeds.append({
                    "title": title,
                    "color": 3447003, # Blue border
                    "fields": fields
                })

    # 3. Smart Payload Packer (Keep under 10 embeds & 5500 chars)
    payloads = []
    current_embeds = []
    current_chars = 0
    
    for embed in all_embeds:
        embed_chars = len(embed["title"])
        for f in embed["fields"]:
            embed_chars += len(f["name"]) + len(f["value"])
            
        if len(current_embeds) >= 10 or (current_chars + embed_chars) > 5500:
            payloads.append({"embeds": current_embeds})
            current_embeds = []
            current_chars = 0
            
        current_embeds.append(embed)
        current_chars += embed_chars
        
    if current_embeds:
        payloads.append({"embeds": current_embeds})
        
    if payloads:
        payloads[0]["content"] = f"🔴 **LIVE TRACKER: {movie_name}**"
        payloads[-1]["embeds"][-1]["footer"] = {
            "text": f"Last Updated: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%I:%M %p')}"
        }

    # 4. Dispatch and Track Multiple Message IDs per Variant
    message_id_key = f"{watch_name}_discord_msg_ids"
    saved_msg_ids = state.get(message_id_key, [])
    if isinstance(saved_msg_ids, str): 
        saved_msg_ids = [saved_msg_ids]
        
    new_msg_ids = []

    for i, payload in enumerate(payloads):
        try:
            url_base = f"{DISCORD_WEBHOOK_URL}?wait=true"
            if threadid:
                url_base += f"&thread_id={threadid}"
                
            if i < len(saved_msg_ids):
                msg_id = saved_msg_ids[i]
                patch_url = f"{DISCORD_WEBHOOK_URL}/messages/{msg_id}"
                if threadid:
                    patch_url += f"?thread_id={threadid}"
                    
                res = requests.patch(patch_url, json=payload, timeout=10)
                if res.status_code in [200, 204]:
                    new_msg_ids.append(msg_id)
                else:
                    print(f"  ⚠️ Discord PATCH failed: {res.text}")
            else:
                res = requests.post(url_base, json=payload, timeout=10)
                if res.status_code in [200, 204, 201]:
                    new_msg_ids.append(res.json()["id"])
                    
            time.sleep(1) # Rate limit protection
        except Exception as e:
            print(f"  ⚠️ Discord Error: {e}")

    # 5. Cleanup leftover messages if shows were removed
    for msg_id in saved_msg_ids[len(payloads):]:
        try:
            del_url = f"{DISCORD_WEBHOOK_URL}/messages/{msg_id}"
            if threadid:
                del_url += f"?thread_id={threadid}"
            requests.delete(del_url, timeout=10)
        except Exception:
            pass

    state[message_id_key] = new_msg_ids

    
def send_telegram(threadid, watch_name, subject, changes, shows, movie_info):
    if not TELEGRAM_BOT_TOKEN or not GROUP_CHAT_ID:
        print("  ⚠️ Telegram skipped — TELEGRAM_BOT_TOKEN or GROUP_CHAT_ID not configured.")
        return

    if not changes:
        return

    now_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %I:%M %p")
    movie_name = movie_info.get("name", watch_name.split('_')[0])
    
    # --- UPGRADED SMART HASHTAG CLEANER ---
    tag_name = re.sub(r'\(.*?\)', '', str(movie_name))
    tag_name = tag_name.title()
    clean_movie_name = re.sub(r'[^A-Za-z0-9]', '', tag_name)

    # --- Extract Language & Format for the Header ---
    lang_fmt_match = re.search(r'\(([^)]+)\)$', str(watch_name))
    lang_fmt_str = f" 🗣️ <b>{lang_fmt_match.group(1)}</b>" if lang_fmt_match else ""

    def clean_price(price_val):
        try:
            return f"₹{int(round(float(price_val)))}"
        except Exception:
            return f"₹{price_val}"

    # 1. ORDERED HASHTAG GENERATOR
    action_tags = set()
    for c in changes:
        c_type = str(c.get('type', '')).upper()
        if c_type:
            action_tags.add(f"#{c_type.replace('_', '')}")

    tag_list = [f"#{clean_movie_name}"] + sorted(list(action_tags))
    hashtag_str = " ".join(tag_list)

    # 2. BETTER SORTING (Date -> Venue -> Time -> Price)
    sorted_changes = sorted(
        changes,
        key=lambda x: (
            str(x.get("date", "")),
            str(x.get("venue", "")).lower(),
            parse_time_to_minutes(x.get("time", "")),
            parse_price(x.get("price", 0))
        )
    )

    # 3. BETTER GROUPING
    nested_data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for item in sorted_changes:
        d_val = item["date"]
        venue = item["venue"]
        show_key = (item["time"], item.get("screen", ""), item.get("vcode", ""), item.get("sid", ""))
        nested_data[d_val][venue][show_key].append(item)

    # 4. BUILD MOBILE-OPTIMIZED HEADER
    lines = [
        f"🚨 <b>BMS Ticket Alert!</b>",
        f"🎬 <b>{html.escape(str(movie_name))}</b>{lang_fmt_str}",
        f"🏷 {hashtag_str}",
        f"🕒 <i>{html.escape(now_str)}</i>\n",
    ]

    # 5. RENDER ULTRA-COMPACT HIERARCHY
    for date_val, venues_dict in nested_data.items():
        formatted_date = format_date(date_val)
        lines.append(f"╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌")
        lines.append(f"📅 <b>{html.escape(str(formatted_date)).upper()}</b>")
        lines.append(f"╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌ ╌")

        for venue, times_dict in venues_dict.items():
            lines.append(f"🏢 <b>{html.escape(str(venue))}</b>")
            
            for (time_val, screen, vcode, sid), items in times_dict.items():
                raw_screen = str(screen).strip() if screen else "NORM"
                if len(raw_screen) > 15:
                    raw_screen = raw_screen[:12] + "..."
                screen_name = html.escape(raw_screen)
                chain_url = CINEMA_CHAIN_URLS.get(vcode)
                
                if chain_url:
                    screen_str = f' [<a href="{chain_url}">{screen_name}</a>]'
                else:
                    venue_upper = venue.upper()
                    if "PVR" in venue_upper:
                        screen_str = f' [<a href="https://www.pvrcinemas.com/">{screen_name}</a>]'
                    elif "INOX" in venue_upper:
                        screen_str = f' [<a href="https://www.inoxmovies.com/">{screen_name}</a>]'
                    else:
                        screen_str = f" [{screen_name}]"
                
                # Direct Deep Link for Time
                time_display = html.escape(str(time_val))
                if vcode and sid:
                    book_url = f"https://in.bookmyshow.com/booktickets/{vcode}/{sid}"
                    time_display = f'<a href="{book_url}"><b>{time_display}</b></a>'
                else:
                    time_display = f"<b>{time_display}</b>"

                lines.append(f"🕒{time_display}{screen_str}")

                # --- PULL ALL CATEGORIES FOR THIS SHOWTIME FROM FULL SNAPSHOT ---
                full_categories = []
                for s in shows:
                    if (str(s.date_code) == str(date_val) and 
                        str(s.venue_code) == str(vcode) and 
                        str(s.session_id) == str(sid)):
                        full_categories = s.categories
                        break
                
                # Fallback to change items if snapshot lookup misses
                categories_to_render = full_categories if full_categories else items

# Sort High to Low. If prices are the same, sort Alphabetically by Category Name.
                categories_to_render = sorted(
    categories_to_render, 
    key=lambda c: (
        -parse_price(c.price if hasattr(c, 'price') else c.get('price', 0)),  # Negative forces High to Low
        str(c.name if hasattr(c, 'name') else c.get('cat', '')).lower()       # Alphabetical tie-breaker
    )
)
                
                
                # Map changes for quick lookup by category name
                change_map = {c.get("cat"): c for c in items}

                for cat in categories_to_render:
                    # Handle object vs dict attributes safely
                    if hasattr(cat, 'name'):
                        cat_name = cat.name
                        cat_price = cat.price
                        cat_status = str(cat.status)
                    else:
                        cat_name = cat.get('cat', '')
                        cat_price = cat.get('price', '0')
                        cat_status = str(cat.get('status', '3'))

                    safe_cat_name = html.escape(str(cat_name).strip())
                    matched_change = change_map.get(cat_name)

                    if matched_change:
                        c_type = matched_change.get("type", "")
                        price = clean_price(matched_change.get('price', cat_price))
                        old_price = clean_price(matched_change.get('old_price', '0'))
                        
                        raw_status = str(matched_change.get('status', cat_status)).strip()
                        raw_old_status = str(matched_change.get('old_status', '')).strip()
                        

                        curr_st, curr_emoji = resolve_status_info(raw_status)
                        old_st, old_emoji = resolve_status_info(raw_old_status) if raw_old_status != "" else ("", "")

                        is_price_change = c_type in ("PRICE_DROP", "PRICE_INCREASE")
                        has_status_transition = (raw_old_status != "" and old_emoji != curr_emoji)

                        if c_type == "NEW":
                            cat_icon = "🆕"
                            detail_line = f"<b>{price}</b> [{curr_st}{curr_emoji}]"
                        elif is_price_change and has_status_transition:
                            cat_icon = "📉🔄" if c_type == "PRICE_DROP" else "📈🔄"
                            status_part = f"[{old_st}{old_emoji}➔{curr_st}{curr_emoji}]"
                            detail_line = f"<s>{old_price}</s>➔<b>{price}</b> {status_part}"
                        elif c_type == "PRICE_DROP":
                            cat_icon = "📉"
                            detail_line = f"<s>{old_price}</s>➔<b>{price}</b> [{curr_st}{curr_emoji}]"
                        elif c_type == "PRICE_INCREASE":
                            cat_icon = "📈"
                            detail_line = f"<s>{old_price}</s>➔<b>{price}</b> [{curr_st}{curr_emoji}]"
                        elif c_type == "RESTOCKED":
                            cat_icon = "🔄"
                            status_part = f"[{old_st}{old_emoji}➔{curr_st}{curr_emoji}]"
                            detail_line = f"<b>{price}</b> {status_part}"
                        else:
                            cat_icon = ""
                            detail_line = f"<b>{price}</b> [{curr_st}{curr_emoji}]"
                    else:
                        price = clean_price(cat_price)
                        curr_st, curr_emoji = resolve_status_info(cat_status)
                        cat_icon = ""
                        detail_line = f"<b>{price}</b> [{curr_st}{curr_emoji}]"

                    # Append Spaced Block Lines with the icon right next to the category name
                    lines.append(f" └🎟️<b>{safe_cat_name}</b> {cat_icon}")
                    lines.append(f"   <code>{detail_line}</code>")
            
                lines.append("") # Visual gap between venues

    # 6. DISPATCH
    message_chunks = split_message_chunks(lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    alert_failed = False
    last_error = "Unknown Error"

    for chunk in message_chunks:
        chunk_success = False
        for attempt in range(1, 4):
            try:
                payload = {
                    "chat_id": GROUP_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": False, 
                }
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

    # 7. ADMIN FALLBACK
    if alert_failed and TELEGRAM_CHAT_ID:
        report_text = (
            f"⚠️ <b>Delivery Failure Report</b>\n"
            f"Failed to deliver alert for <b>{html.escape(str(movie_name))}</b> to Topic ID <code>{threadid}</code> in the Group.\n"
            f"❌ <b>Reason:</b> <code>{html.escape(last_error)}</code>"
        )
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": report_text, "parse_mode": "HTML"}, timeout=10)
            for chunk in message_chunks:
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
                time.sleep(1)
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
        discord_thread_id,
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
    date_time_map,   # <--- ADD THIS HERE
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
    successful_date_codes = set()
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
            label,
        )

        if data:
            # BMS successfully responded for this requested date.
    # This date is safe to use for missing-show detection.
            if date_code:
              successful_date_codes.add(str(date_code))

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
            # Use the clean URL name as a safety fallback!
            clean_fallback = label.split('_')[0]
            movie_info = parse_movie_info(data, fallback_name=clean_fallback)

        returned_dates = parse_dates(data)
        all_dates.extend(returned_dates)
        all_shows.extend(parse_shows(data))

# For the default-date API request, use the dates actually
# returned by BMS as the successfully checked dates.
        if not date_code:
           successful_date_codes.update(
        str(d.date_code)
        for d in returned_dates
        if d.date_code
    )

    if not successful_date_codes:


        print("  ⚠️ No successfully checked dates.")


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
        date_time_map,   # <--- ADD THIS HERE
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
    
    # --- GRACE PERIOD LOGIC FOR MISSING SHOWS ---
        # ==============================================================
    # SHOW REMOVAL / MISSING SHOW GRACE LOGIC
    #
    # IMPORTANT:
    # - State is stored per ticket category.
    # - A physical show is only considered missing when ALL of its
    #   categories are absent from a SUCCESSFUL BMS response.
    # - SOLD categories are NOT treated as removed.
    # - API/server failures must NOT increment missing_count.
    # ==============================================================

    removed_shows = []

    if old_watch_state and "shows" in old_watch_state:

        old_shows = old_watch_state["shows"]
        new_shows = new_watch_state["shows"]

        MAX_MISSING_CHECKS = 3

        # ----------------------------------------------------------
        # Build physical-show keys from the NEW successful snapshot.
        #
        # Category is intentionally NOT part of this key.
        # ----------------------------------------------------------
        new_physical_shows = set()

        for new_show in new_shows.values():

            physical_key = (
                str(new_show.get("date", "")),
                str(new_show.get("venue", "")),
                str(new_show.get("time", "")),
                str(new_show.get("vcode", "")),
                str(new_show.get("sid", "")),
            )

            new_physical_shows.add(physical_key)

        # ----------------------------------------------------------
        # Track which physical shows we already processed.
        #
        # This is important because the state contains one entry
        # for every ticket category.
        # ----------------------------------------------------------
        processed_physical_shows = set()

        # ----------------------------------------------------------
        # Check every old category entry.
        # ----------------------------------------------------------
        for key, old_show in old_shows.items():

            physical_key = (
                str(old_show.get("date", "")),
                str(old_show.get("venue", "")),
                str(old_show.get("time", "")),
                str(old_show.get("vcode", "")),
                str(old_show.get("sid", "")),
            )

            # Same physical show can exist multiple times because
            # each category is stored separately.
            if physical_key in processed_physical_shows:
                continue

            processed_physical_shows.add(physical_key)

            # ------------------------------------------------------
            # IMPORTANT:
            # Only compare shows belonging to dates that BMS
            # successfully responded for.
            # ------------------------------------------------------
            old_show_date = str(old_show.get("date", ""))

            if old_show_date not in successful_date_codes:
                new_shows[key] = old_show
                continue

            # ------------------------------------------------------
            # Ignore shows that have already started/passed.
            # cleanup_state() handles old dates separately.
            # ------------------------------------------------------
            if not is_show_in_future(
                old_show.get("date", ""),
                old_show.get("time", ""),
            ):
                continue

            # ------------------------------------------------------
            # If the physical show still exists in the NEW snapshot,
            # then it is NOT removed.
            #
            # This also covers:
            #   Category A = SOLD
            #   Category B = FEW
            #   Category C = AVL
            #
            # The show remains alive.
            # ------------------------------------------------------
            if physical_key in new_physical_shows:

                # Reset missing count for every category belonging
                # to this physical show.
                for old_key, old_category in old_shows.items():

                    old_physical_key = (
                        str(old_category.get("date", "")),
                        str(old_category.get("venue", "")),
                        str(old_category.get("time", "")),
                        str(old_category.get("vcode", "")),
                        str(old_category.get("sid", "")),
                    )

                    if old_physical_key == physical_key:
                        old_category["missing_count"] = 0

                continue

            # ------------------------------------------------------
            # Physical show is absent from the successful BMS
            # snapshot.
            #
            # Since build_state() contains every category, reaching
            # here means ALL previously tracked categories disappeared.
            # ------------------------------------------------------

            current_missing = old_show.get(
                "missing_count",
                0,
            ) + 1

            # ------------------------------------------------------
            # Update missing_count for ALL categories belonging to
            # this same physical show.
            #
            # This makes the grace counter represent the SHOW,
            # not an individual category.
            # ------------------------------------------------------
            for old_key, old_category in old_shows.items():

                old_physical_key = (
                    str(old_category.get("date", "")),
                    str(old_category.get("venue", "")),
                    str(old_category.get("time", "")),
                    str(old_category.get("vcode", "")),
                    str(old_category.get("sid", "")),
                )

                if old_physical_key == physical_key:

                    old_category["missing_count"] = current_missing

                    # Keep the categories during the grace period.
                    if current_missing <= MAX_MISSING_CHECKS:
                        new_shows[old_key] = old_category

            # ------------------------------------------------------
            # Show has survived the complete grace period.
            # ------------------------------------------------------
            if current_missing > MAX_MISSING_CHECKS:

                print(
                    f"  🗑️ SHOW REMOVED after "
                    f"{MAX_MISSING_CHECKS} consecutive "
                    f"successful missing checks: "
                    f"{old_show.get('venue', 'Unknown')} "
                    f"{old_show.get('time', '')}"
                )

                # One physical show only.
                removed_shows.append({
                    "date": old_show.get("date", ""),
                    "venue": old_show.get("venue", ""),
                    "time": old_show.get("time", ""),
                    "screen": old_show.get("screen", ""),
                    "vcode": old_show.get("vcode", ""),
                    "sid": old_show.get("sid", ""),
                })

                # Remove ALL category entries belonging to this
                # physical show from the new state.
                keys_to_remove = []

                for old_key, old_category in new_shows.items():

                    new_physical_key = (
                        str(old_category.get("date", "")),
                        str(old_category.get("venue", "")),
                        str(old_category.get("time", "")),
                        str(old_category.get("vcode", "")),
                        str(old_category.get("sid", "")),
                    )

                    if new_physical_key == physical_key:
                        keys_to_remove.append(old_key)

                for old_key in keys_to_remove:
                    new_shows.pop(old_key, None)

    # changes = []

    # if old_watch_state:
    #     changes = detect_changes(
    #         old_watch_state,
    #         new_watch_state,
    #     )

    # REMOVED the "if old_watch_state:" restriction 
    # so it alerts on the very first run too!
        # ==============================================================
    # SEND REMOVED-SHOW ALERT
    # ==============================================================
    # Use the actual language/format returned by BMS for alerts
    alert_label = label

    raw_alert_lang = str(movie_info.get("language", "")).strip()

    if "•" in raw_alert_lang:
      parts = [p.strip() for p in raw_alert_lang.split("•")]
      if len(parts) >= 2:
        alert_tag = f"({parts[0]} {parts[1]})"

        if not str(alert_label).endswith(alert_tag):
            clean_base = re.sub(
                r'(?i)(Telugu|Tamil|Hindi|Malayalam|English)',
                '',
                str(alert_label).split('_')[0]
            )

            try:
                timestamp_id = str(alert_label).split('_')[1].split(' ')[0]
            except Exception:
                timestamp_id = "000000"

            alert_label = f"{clean_base}_{timestamp_id} {alert_tag}"

    if removed_shows:

        send_removed_shows_telegram(
            threadid,
            alert_label,
            removed_shows,
            movie_info,
        )

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
            alert_label,
                        (
                            f"BMS Alert: "
                            f"{movie_info['name']} - "
                            f"{len(changes)} change(s)"
                        ),
                        changes,
                        filtered,
                        movie_info,
        )

        send_ntfy(label, movie_info, changes)

        # --- NEW DISCORD INSTANT PING ---
        if DISCORD_WEBHOOK_URL:
            ping_payload = {
                "content": f"🚨 **TICKET ALERT:** New shows or restocks for **{movie_info['name']}**! Check the live board above."
            }
            ping_url = f"{DISCORD_WEBHOOK_URL}?thread_id={discord_thread_id}" if threadid else DISCORD_WEBHOOK_URL
            try:
                requests.post(ping_url, json=ping_payload, timeout=10)
            except Exception:
                pass

    else:
        print("  ✅ No changes since last check.")

    # --- NEW SILENT DISCORD DASHBOARD UPDATE ---
    if filtered:
        update_discord_dashboard(discord_thread_id, alert_label, filtered, movie_info, state)

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
    discord_thread_id=watch["discord_thread_id"]

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

    # --------------------------------------------------------------
    # Base event
    # --------------------------------------------------------------

    state, success, first_full_data = run_event(
        discord_thread_id=discord_thread_id,
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
        date_time_map=watch.get("date_time_map", {}),  # <--- ADD THIS HERE
        state=state,
        save_raw_prefix=(
            f"bms_response_{watch_name}"
        ),
    )

    overall_success = success

    # ==================================================================
    # DYNAMIC API RENAMING & LANGUAGE FILTERING 
    # ==================================================================
    if first_full_data:
        # Pass the clean URL name to prevent "Unknown Movie" loops
        clean_fallback = watch_name.split('_')[0]
        base_info = parse_movie_info(first_full_data, fallback_name=clean_fallback)
        raw_lang = str(base_info.get("language", "")).strip()
        base_lang = raw_lang.lower()
        
        # 1. RENAME TRACKER BASED ON TRUE API DATA
        if "•" in raw_lang:
            parts = [p.strip() for p in raw_lang.split("•")]
            if len(parts) >= 2:
                api_lang = parts[0]
                api_fmt = parts[1]
                correct_tag = f"({api_lang} {api_fmt})"
                
                # If the tracker name doesn't match the live API, fix it!
                if correct_tag not in watch_name:
                    # Strip out wrong language words from the base name
                    clean_base = re.sub(r'(?i)(Telugu|Tamil|Hindi|Malayalam|English)', '', watch_name.split('_')[0])
                    
                    # Keep the original timestamp ID so Telegram /stop works
                    try:
                        timestamp_id = watch_name.split('_')[1].split(' ')[0]
                    except:
                        timestamp_id = "000000"
                        
                    new_watch_name = f"{clean_base}_{timestamp_id} {correct_tag}"
                    print(f"  ✨ API Match! Fixing base tracker name to: {new_watch_name}")
                    
                    # Update live state
                    if watch_name in state:
                        state[new_watch_name] = state.pop(watch_name)
                        
                    # Update watches.json permanently
                    try:
                        with open(WATCHES_FILE, "r", encoding="utf-8") as f:
                            all_watches = json.load(f)
                        for w in all_watches:
                            if w.get("name") == watch_name:
                                w["name"] = new_watch_name
                                break
                        with open(WATCHES_FILE, "w", encoding="utf-8") as f:
                            json.dump(all_watches, f, indent=2, ensure_ascii=False)
                    except Exception as e:
                        print(f"  ⚠️ Could not save new name to watches.json: {e}")
                    
                    # Apply the new name to the active script variables
                    watch["name"] = new_watch_name
                    watch_name = new_watch_name
        
        # 2. APPLY USER'S LANGUAGE FILTERS
        if watch.get("languages"):
            base_lang = raw_lang.lower()
            if not any(lang in base_lang for lang in watch["languages"]):
                print(
                    f"  ⚠️ Skipping base watch state: language "
                    f"('{raw_lang}') not in allowed list {watch['languages']}"
                )
                state.pop(watch_name, None)
    # ==================================================================

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
                # Reconstruct standardized URL format: https://in.bookmyshow.com/movies/{region}/{title_slug}/{event_code}
                new_url = f"https://in.bookmyshow.com/movies/{clean_region}/{title_slug}/{v_code}"

                if watch["url"] != new_url:
                    print(
                        f"  💡 Auto-updating watch URL to target variant directly: {new_url}"
                    )
                    watch["url"] = new_url
                    
                    # 🔥 FIX: Synchronize the base watch name with the new variant immediately
                    correct_tag = f"({target_variant.language} {target_variant.format})"
                    clean_base = re.sub(r'(?i)(Telugu|Tamil|Hindi|Malayalam|English)', '', watch_name.split('_')[0])
                    
                    try:
                        timestamp_id = watch_name.split('_')[1].split(' ')[0]
                    except Exception:
                        timestamp_id = "000000"
                        
                    new_watch_name = f"{clean_base}_{timestamp_id} {correct_tag}"
                    
                    # 🔥 STATE MIGRATION: Fix the broken state keys so we don't spam duplicate alerts!
                    old_stacked_name = f"{watch_name} {correct_tag}"
                    if old_stacked_name in state:
                        state[new_watch_name] = state.pop(old_stacked_name)
                    elif watch_name in state:
                        state[new_watch_name] = state.pop(watch_name)
                        
                    print(f"  💡 Pre-emptively fixing base watch name to match variant: {new_watch_name}")
                    
                    old_watch_name = watch_name
                    watch["name"] = new_watch_name
                    watch_name = new_watch_name # Update for the rest of the loop

                    try:
                        if os.path.exists(WATCHES_FILE):
                            with open(WATCHES_FILE, "r", encoding="utf-8") as f:
                                watches_data = json.load(f)

                            for w in watches_data:
                                if w.get("name") == old_watch_name:
                                    w["url"] = new_url
                                    w["name"] = new_watch_name # 🔥 Save the new name permanently!

                            with open(WATCHES_FILE, "w", encoding="utf-8") as f:
                                json.dump(watches_data, f, indent=2, ensure_ascii=False)
                    except Exception as e:
                        print(f"  ⚠️ Could not update {WATCHES_FILE}: {e}")

            for variant in selected:

                variant_tag = f"({variant.language} {variant.format})"

                if watch_name.endswith(variant_tag):
                  variant_name = watch_name
                else:
                  variant_name = f"{watch_name} {variant_tag}"

                print(
                    f"\n  --- Variant: {variant_name} "
                    f"[{variant.event_code}] ---"
                )

                state, variant_success, _ = run_event(
                    discord_thread_id=discord_thread_id,
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
                    date_time_map=watch.get("date_time_map", {}),  # <--- ADD THIS HERE
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
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] BMS Ticket Checker — CI mode")

    watches = load_watches()
    print(f"📋 Loaded {len(watches)} watch(es)")

    state = load_state()
    state = cleanup_state(state)

    successful = 0
    watches_updated = False
    
    # Get current time for 14-day cleanup comparison
    current_time = time.time()
    FOURTEEN_DAYS_SECONDS = 14 * 86400
    today_int = int(datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d"))

    surviving_watches = []

    for idx, watch in enumerate(watches):
        status = watch.get("status")
        closed_at = watch.get("closed_at", 0)
        watch_name = watch.get("name", "Unknown")
        
        # --- 1. 14-DAY PASSIVE CLEANUP FOR CLOSED WATCHES ---
        if status == "closed":
            surviving_watches.append(watch) # Retain in list during the 14-day window
            
            if closed_at and (current_time - closed_at) > FOURTEEN_DAYS_SECONDS:
                thread_id = watch.get("message_thread_id")
                discord_thread_id = watch.get("discord_thread_id") # <--- Grab Discord Thread ID
                
                # A. Permanently delete the topic from Telegram
                if thread_id and GROUP_CHAT_ID and TELEGRAM_BOT_TOKEN:
                    try:
                        del_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteForumTopic"
                        payload = {
                            "chat_id": GROUP_CHAT_ID,
                            "message_thread_id": thread_id
                        }
                        res = requests.post(del_url, json=payload, timeout=10)
                        if res.status_code == 200:
                            print(f"\n🗑️ 14-day retention expired. Purged topic ID {thread_id} for '{watch_name}'")
                        else:
                            print(f"\n⚠️ Failed to purge topic for '{watch_name}': {res.text}")
                    except Exception as e:
                        print(f"\n⚠️ Error deleting forum topic: {e}")
                # A2. Permanently delete the thread from Discord (NEW)
                if discord_thread_id:
                    try:
                        delete_discord_forum_thread(discord_thread_id)
                        print(f"\n🗑️ 14-day retention expired. Purged Discord thread ID {discord_thread_id} for '{watch_name}'")
                    except Exception as e:
                        print(f"\n⚠️ Error deleting Discord thread: {e}")

                # B. Purge any residual keys from state.json (bms_state.json)
                try:
                    timestamp_match = re.search(r'_(\d{10,})', watch_name)
                    unique_id = timestamp_match.group(1) if timestamp_match else watch_name.split('_')[0]
                    
                    keys_to_delete = [k for k in state.keys() if unique_id in str(k)]
                    if keys_to_delete:
                        for k in keys_to_delete:
                            del state[k]
                        print(f"🧹 Cleaned up {len(keys_to_delete)} residual state record(s) for expired watch ID {unique_id}")
                except Exception as e:
                    print(f"⚠️ Error cleaning residual state for '{watch_name}': {e}")
                
                print(f"🧹 Removing closed watch '{watch_name}' permanently from watches.json.")
                surviving_watches.pop() # Drop it from the surviving list
                watches_updated = True
            
            continue  # CRITICAL: Skip all active checks and searches for closed watches!

        # --- 2. REGULAR EXPIRY CHECK FOR ACTIVE WATCHES ---
        configured_dates = watch.get("dates", [])
        
        if configured_dates and all(str(d).isdigit() and int(d) < today_int for d in configured_dates):
            if not watch.get("expired_notified"):
                print(f"\n  ⏰ Watch '{watch['name']}' has expired. Sending notification...")
                send_watch_expiry_alert(watch, idx)
                watch["expired_notified"] = True
                watches_updated = True
            else:
                print(f"\n  ⏭️ Skipping expired watch '{watch['name']}' (Waiting for manual close).")
            
            surviving_watches.append(watch)
            continue

        # --- 3. RUN ACTIVE WATCH ---
        surviving_watches.append(watch)
        try:
            state, success = run_watch(watch, state)
            if success:
                successful += 1

        except Exception as e:
            print("")
            print(f"❌ Watch '{watch['name']}' failed:")
            print(f"    {type(e).__name__}: {e}")
            continue

    # Save state and updated watches list if any changes occurred
    save_state(state)
    if watches_updated:
        save_watches(surviving_watches)

    print("")
    print("=" * 70)
    print(f"✅ Completed: {successful}/{len(surviving_watches)} active watch(es)")
    print("=" * 70)


if __name__ == "__main__":
    main()
