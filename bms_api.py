from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


log = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v5/"
    "showtimes-by-event/primary-dynamic"
)

BASE = "https://in.bookmyshow.com"

REGION_MAP = {
    "chennai": (
        "CHEN",
        "chennai",
        "13.056",
        "80.206",
        "tf3",
    ),
}


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_name: str
    venue_code: str
    time: str
    screen_attr: str

    categories: List[CatInfo] = field(
        default_factory=list
    )

    language: str = ""
    movie_format: str = ""
    language_format_text: str = ""


@dataclass
class Venue:
    name: str
    showtimes: List[str] = field(
        default_factory=list
    )


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "application/json, text/plain, */*"
        ),
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Referer": (
            "https://in.bookmyshow.com/"
        ),
        "Origin": (
            "https://in.bookmyshow.com"
        ),
        "Connection": "keep-alive",
    }
)


# ============================================================
# TEXT HELPERS
# ============================================================

def _clean_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text


def _normalize_format(value: Any) -> str:
    text = _clean_text(
        value
    ).upper()

    if not text:
        return ""

    compact = text.replace(
        " ",
        "",
    )

    aliases = {
        "2D": "2D",
        "3D": "3D",
        "4DX": "4DX",
        "IMAX": "IMAX",
        "IMAX2D": "IMAX",
        "IMAX3D": "IMAX",
        "MX4D": "MX4D",
        "DBOX": "D-BOX",
        "D-BOX": "D-BOX",
    }

    return aliases.get(
        compact,
        text,
    )


# ============================================================
# COMBO NORMALIZATION
# ============================================================

def _normalize_combo(
    combo: Optional[str],
) -> Optional[str]:

    if combo is None:
        return None

    combo = _clean_text(
        combo
    )

    if not combo:
        return None

    if combo.lower() == "any":
        return None

    # Supported:
    #
    # Malayalam - 2D
    # Malayalam|2D
    # Malayalam • 2D
    # Malayalam – 2D
    # Malayalam — 2D

    parts = re.split(
        r"\s*[-–—·|]\s*",
        combo,
        maxsplit=1,
    )

    if len(parts) >= 2:

        language = _clean_text(
            parts[0]
        )

        movie_format = _normalize_format(
            parts[1]
        )

        if language and movie_format:
            return (
                f"{language} - "
                f"{movie_format}"
            )

    return combo


def _combo_parts(
    combo: Optional[str],
) -> tuple[str, str]:

    normalized = _normalize_combo(
        combo
    )

    if normalized is None:
        return "", ""

    parts = re.split(
        r"\s*-\s*",
        normalized,
        maxsplit=1,
    )

    if len(parts) < 2:
        return "", ""

    return (
        _clean_text(parts[0]),
        _normalize_format(parts[1]),
    )


def _canonical_combo(
    value: str,
) -> str:

    if not value:
        return ""

    text = _clean_text(
        value
    ).lower()

    # Remove everything after screen information.
    #
    # Malayalam • 2D | SCREEN 5
    # becomes:
    #
    # Malayalam • 2D

    text = text.split(
        "|",
        1,
    )[0].strip()

    # Normalize BMS separators.
    text = text.replace(
        "•",
        "|",
    )

    text = text.replace(
        "–",
        "-",
    )

    text = text.replace(
        "—",
        "-",
    )

    text = re.sub(
        r"\s*-\s*",
        "|",
        text,
        count=1,
    )

    text = re.sub(
        r"\s*\|\s*",
        "|",
        text,
    )

    parts = [
        part.strip()
        for part in text.split("|")
        if part.strip()
    ]

    if len(parts) >= 2:

        language = parts[0]

        movie_format = (
            _normalize_format(
                parts[1]
            ).lower()
        )

        return (
            f"{language}|"
            f"{movie_format}"
        )

    return text


def _showtime_matches_combo(
    language_format_text: str,
    combo: str,
) -> bool:

    actual = _canonical_combo(
        language_format_text
    )

    requested = _canonical_combo(
        combo
    )

    matched = (
        bool(actual)
        and bool(requested)
        and actual == requested
    )

    log.debug(
        "COMBO MATCH | actual=%r | "
        "requested=%r | matched=%s",
        actual,
        requested,
        matched,
    )

    return matched


# ============================================================
# DATE
# ============================================================

def normalize_date_code(
    value: Any,
) -> str:

    if isinstance(
        value,
        datetime,
    ):
        return value.strftime(
            "%Y%m%d"
        )

    if isinstance(
        value,
        date_type,
    ):
        return value.strftime(
            "%Y%m%d"
        )

    text = str(
        value
    ).strip()

    if not text:
        raise ValueError(
            "date_code cannot be empty"
        )

    if re.fullmatch(
        r"\d{8}",
        text,
    ):
        return text

    for fmt in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
    ):

        try:

            return datetime.strptime(
                text,
                fmt,
            ).strftime(
                "%Y%m%d"
            )

        except ValueError:
            continue

    try:

        return datetime.fromisoformat(
            text.replace(
                "Z",
                "+00:00",
            )
        ).strftime(
            "%Y%m%d"
        )

    except ValueError:
        pass

    raise ValueError(
        f"Unsupported date_code: {value!r}"
    )


# ============================================================
# REGION
# ============================================================

def _get_region(
    region_slug: str,
):

    region_slug = _clean_text(
        region_slug
    ).lower()

    if region_slug not in REGION_MAP:

        raise ValueError(
            f"Unsupported BMS region: "
            f"{region_slug}"
        )

    return REGION_MAP[
        region_slug
    ]


def resolve_region(
    region_slug: str,
):

    (
        region_code,
        sub_code,
        lat,
        lon,
        geohash,
    ) = _get_region(
        region_slug
    )

    return (
        region_code,
        sub_code,
        lat,
        lon,
        geohash,
    )


# ============================================================
# BMS URL
# ============================================================

def parse_bms_url(
    url: str,
) -> Dict[str, str]:

    url = _clean_text(
        url
    )

    if not url:

        raise ValueError(
            "BookMyShow URL is empty"
        )

    parsed = urlparse(
        url
    )

    path_parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    event_code = ""

    for part in reversed(
        path_parts
    ):

        if re.fullmatch(
            r"ET\d+",
            part,
            re.IGNORECASE,
        ):

            event_code = (
                part.upper()
            )

            break

    if not event_code:

        raise ValueError(
            "Could not find BMS event "
            "code in URL"
        )

    region_slug = "chennai"
    movie_slug = ""

    try:

        index = path_parts.index(
            "movies"
        )

        if (
            len(path_parts)
            > index + 1
        ):

            region_slug = (
                path_parts[
                    index + 1
                ].lower()
            )

        if (
            len(path_parts)
            > index + 2
        ):

            movie_slug = (
                path_parts[
                    index + 2
                ].lower()
            )

    except ValueError:
        pass

    log.info(
        "PARSED URL | event=%s | "
        "region=%s | movie=%s",
        event_code,
        region_slug,
        movie_slug,
    )

    return {
        "event_code": event_code,
        "region_slug": region_slug,
        "movie_slug": movie_slug,
    }


def movie_name_from_slug(
    slug: str,
) -> str:

    slug = _clean_text(
        slug
    )

    if not slug:
        return "Movie"

    return slug.replace(
        "-",
        " ",
    ).title()


# ============================================================
# HEADERS
# ============================================================

def _bms_headers(
    region_code: str,
    region_slug: str,
    lat: str,
    lon: str,
    geohash: str,
) -> Dict[str, str]:

    return {
        "Accept": (
            "application/json, "
            "text/plain, */*"
        ),
        "User-Agent": session.headers[
            "User-Agent"
        ],
        "x-app-code": "WEB",
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-location-selection": "manual",
        "x-longitude": lon,
        "x-platform": "WEB",
        "x-platform-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
    }


# ============================================================
# FETCH BMS
# ============================================================

def fetch_bms(
    event_code: str,
    region_slug: str,
    date_code,
    region_code: Optional[str] = None,
    lat: Optional[str] = None,
    lon: Optional[str] = None,
    geohash: Optional[str] = None,
    language: Optional[str] = None,
    ref_event_code: Optional[str] = None,
) -> Dict[str, Any]:

    date_code = normalize_date_code(
        date_code
    )

    (
        default_region_code,
        _sub_code,
        default_lat,
        default_lon,
        default_geohash,
    ) = _get_region(
        region_slug
    )

    region_code = (
        region_code
        or default_region_code
    )

    lat = lat or default_lat
    lon = lon or default_lon
    geohash = (
        geohash
        or default_geohash
    )

    event_code = _clean_text(
        event_code
    ).upper()

    ref_event_code = (
        _clean_text(
            ref_event_code
        ).upper()
        or event_code
    )

    language = _clean_text(
        language
    ).lower()

    params = {
        "etCodes": event_code,
        "dateCode": date_code,
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "appCode": "WEB",
        "language": language,
        "refEventCode": ref_event_code,
    }

    headers = _bms_headers(
        region_code=region_code,
        region_slug=region_slug,
        lat=lat,
        lon=lon,
        geohash=geohash,
    )

    log.info(
        "FETCH BMS | event=%s | ref=%s | "
        "region=%s | date=%s | language=%s",
        event_code,
        ref_event_code,
        region_slug,
        date_code,
        language,
    )

    log.info(
        "BMS PARAMS | %s",
        params,
    )

    try:

        response = session.get(
            API_URL,
            params=params,
            headers=headers,
            timeout=30,
        )

    except requests.RequestException as exc:

        log.exception(
            "BMS REQUEST FAILED"
        )

        raise RuntimeError(
            f"BookMyShow request failed: "
            f"{exc}"
        ) from exc

    log.info(
        "BMS RESPONSE | status=%s",
        response.status_code,
    )

    log.info(
        "BMS FINAL URL | %s",
        response.url,
    )

    if response.status_code != 200:

        raise RuntimeError(
            "BookMyShow returned HTTP "
            f"{response.status_code}: "
            f"{response.text[:2000]}"
        )

    try:

        data = response.json()

    except ValueError as exc:

        try:

            with open(
                "bms_response_debug.txt",
                "w",
                encoding="utf-8",
            ) as file:

                file.write(
                    response.text
                )

        except Exception:
            log.exception(
                "Could not save raw BMS response"
            )

        raise RuntimeError(
            "BookMyShow returned invalid JSON"
        ) from exc

    # Save latest response.
    try:

        with open(
            "bms_response_debug.json",
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                indent=2,
                ensure_ascii=False,
            )

    except Exception:
        log.exception(
            "Could not save debug JSON"
        )

    return data


# ============================================================
# EVENT VARIANTS
# ============================================================

def extract_event_variants(
    data: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:

    variants: Dict[
        str,
        Dict[str, Any],
    ] = {}

    if not isinstance(
        data,
        dict,
    ):
        return variants

    root = data.get(
        "data",
        data,
    )

    if not isinstance(
        root,
        dict,
    ):
        return variants

    bottom_sheet = root.get(
        "bottomSheetData",
        {},
    )

    if not isinstance(
        bottom_sheet,
        dict,
    ):
        return variants

    selector = bottom_sheet.get(
        "format-selector",
        {},
    )

    if not isinstance(
        selector,
        dict,
    ):
        return variants

    widgets = selector.get(
        "widgets",
        [],
    )

    if not isinstance(
        widgets,
        list,
    ):
        return variants

    for widget in widgets:

        if not isinstance(
            widget,
            dict,
        ):
            continue

        if widget.get(
            "type"
        ) != "chip-list":
            continue

        language = _clean_text(
            widget.get(
                "text"
            )
        )

        if not language:
            continue

        chips = widget.get(
            "data",
            [],
        )

        if not isinstance(
            chips,
            list,
        ):
            continue

        for chip in chips:

            if not isinstance(
                chip,
                dict,
            ):
                continue

            movie_format = (
                _normalize_format(
                    chip.get(
                        "title"
                    )
                )
            )

            if not movie_format:
                continue

            cta = chip.get(
                "cta",
                {},
            )

            if not isinstance(
                cta,
                dict,
            ):
                continue

            additional = cta.get(
                "additionalData",
                {},
            )

            if not isinstance(
                additional,
                dict,
            ):
                additional = {}

            analytics = cta.get(
                "analytics",
                {},
            )

            if not isinstance(
                analytics,
                dict,
            ):
                analytics = {}

            event_code = (
                additional.get(
                    "eventCode"
                )
                or analytics.get(
                    "event_code"
                )
            )

            if not event_code:
                continue

            event_code = (
                _clean_text(
                    event_code
                ).upper()
            )

            if not re.fullmatch(
                r"ET\d+",
                event_code,
            ):
                continue

            ref_event_code = (
                _clean_text(
                    additional.get(
                        "refEventCode"
                    )
                    or event_code
                ).upper()
            )

            event_url = _clean_text(
                additional.get(
                    "eventUrl"
                )
            )

            disabled_value = (
                additional.get(
                    "isDisabled",
                    False,
                )
            )

            disabled = (
                disabled_value is True
                or str(
                    disabled_value
                ).lower()
                == "true"
            )

            key = (
                f"{language}|"
                f"{movie_format}"
            )

            variants[key] = {
                "language": language,
                "format": movie_format,
                "event_code": event_code,
                "event_url": event_url,
                "ref_event_code": ref_event_code,
                "disabled": disabled,
            }

    log.info(
        "EVENT VARIANTS | %s",
        variants,
    )

    return variants


# ============================================================
# RESOLVE VARIANT
# ============================================================
def resolve_variant(
    event_code: str,
    region_slug: str,
    combo: Optional[str],
    date_code=None,
) -> tuple[str, str, str]:

    event_code = _clean_text(
        event_code
    ).upper()

    normalized_combo = _normalize_combo(
        combo
    )

    if normalized_combo is None:
        return (
            event_code,
            "",
            event_code,
        )

    (
        requested_language,
        requested_format,
    ) = _combo_parts(
        normalized_combo
    )

    if (
        not requested_language
        or not requested_format
    ):
        raise ValueError(
            f"Invalid combo: {combo!r}"
        )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Resolve the variant using the SAME DATE that the caller
    # is actually watching.
    #
    # Previously this used datetime.now(), which caused a
    # future-date watch to inspect today's selector.
    # --------------------------------------------------------

    if date_code is None:
        date_code = datetime.now()

    target_date_code = normalize_date_code(
        date_code
    )

    log.info(
        "RESOLVE VARIANT | event=%s | "
        "date=%s | combo=%s",
        event_code,
        target_date_code,
        normalized_combo,
    )

    data = fetch_bms(
        event_code=event_code,
        region_slug=region_slug,
        date_code=target_date_code,
        language=None,
        ref_event_code=event_code,
    )

    variants = extract_event_variants(
        data
    )

    wanted_key = (
        f"{requested_language}|"
        f"{requested_format}"
    ).lower()

    selected = None

    for variant in variants.values():
        candidate = (
            f"{_clean_text(variant.get('language'))}|"
            f"{_normalize_format(variant.get('format'))}"
        ).lower()

        if candidate == wanted_key:
            selected = variant
            break

    if selected is None:
        available = ", ".join(
            variants.keys()
        )

        raise ValueError(
            f"BookMyShow did not expose "
            f"{normalized_combo!r} for date "
            f"{target_date_code}.\n"
            f"Available variants: "
            f"{available or 'none'}"
        )

    selected_event = (
        _clean_text(
            selected.get(
                "event_code"
            )
        ).upper()
    )

    selected_language = (
        _clean_text(
            selected.get(
                "language"
            )
        ).lower()
    )

    selected_ref = (
        _clean_text(
            selected.get(
                "ref_event_code"
            )
            or event_code
        ).upper()
    )

    disabled = bool(
        selected.get(
            "disabled",
            False,
        )
    )

    log.info(
        "VARIANT RESOLVED | combo=%s | "
        "event=%s | language=%s | "
        "ref=%s | disabled=%s | date=%s",
        normalized_combo,
        selected_event,
        selected_language,
        selected_ref,
        disabled,
        target_date_code,
    )

    # IMPORTANT:
    # Do not reject disabled=True.
    #
    # A variant can be marked disabled in the selector while
    # still having actual shows on the requested future date.

    if disabled:
        log.warning(
            "Variant is marked disabled by BMS selector, "
            "but will still be checked for requested date=%s.",
            target_date_code,
        )

    return (
        selected_event,
        selected_language,
        selected_ref,
    )

# ============================================================
# FORMAT EXTRACTION
# ============================================================

def _extract_format_text(
    show: Dict[str, Any],
) -> str:

    custom = show.get(
        "customGestureCTA",
        {},
    )

    if not isinstance(
        custom,
        dict,
    ):
        return ""

    additional = custom.get(
        "additionalData",
        {},
    )

    if not isinstance(
        additional,
        dict,
    ):
        return ""

    bottom_sheet = additional.get(
        "bottomSheetData",
        {},
    )

    if not isinstance(
        bottom_sheet,
        dict,
    ):
        return ""

    widgets = bottom_sheet.get(
        "widgets",
        [],
    )

    if not isinstance(
        widgets,
        list,
    ):
        return ""

    for widget in widgets:

        if not isinstance(
            widget,
            dict,
        ):
            continue

        if widget.get(
            "layoutId"
        ) != "format-container":
            continue

        variable = widget.get(
            "variableData",
            {},
        )

        if not isinstance(
            variable,
            dict,
        ):
            continue

        value = variable.get(
            "format"
        )

        if value:

            return _clean_text(
                value
            )

    return ""


# ============================================================
# LANGUAGE / FORMAT PARSER
# ============================================================

def _parse_language_format(
    format_text: str,
) -> tuple[str, str]:

    if not format_text:
        return "", ""

    text = _clean_text(
        format_text
    )

    # Remove screen name.
    #
    # Malayalam • 2D | SCREEN 5
    #
    # -> Malayalam • 2D

    base = text.split(
        "|",
        1,
    )[0].strip()

    if "•" in base:

        language, movie_format = (
            base.split(
                "•",
                1,
            )
        )

    elif " - " in base:

        language, movie_format = (
            base.split(
                " - ",
                1,
            )
        )

    else:

        return base, ""

    return (
        _clean_text(language),
        _normalize_format(
            movie_format
        ),
    )


# ============================================================
# CATEGORIES
# ============================================================

def _extract_categories(
    show: Dict[str, Any],
) -> List[CatInfo]:

    categories: List[
        CatInfo
    ] = []

    custom = show.get(
        "customGestureCTA",
        {},
    )

    if not isinstance(
        custom,
        dict,
    ):
        return categories

    additional = custom.get(
        "additionalData",
        {},
    )

    if not isinstance(
        additional,
        dict,
    ):
        return categories

    bottom_sheet = additional.get(
        "bottomSheetData",
        {},
    )

    if not isinstance(
        bottom_sheet,
        dict,
    ):
        return categories

    widgets = bottom_sheet.get(
        "widgets",
        [],
    )

    if not isinstance(
        widgets,
        list,
    ):
        return categories

    for widget in widgets:

        if not isinstance(
            widget,
            dict,
        ):
            continue

        layout_id = widget.get(
            "layoutId"
        )

        if not str(
            layout_id
        ).startswith(
            "seat-category-type-"
        ):
            continue

        variable = widget.get(
            "variableData",
            {},
        )

        if not isinstance(
            variable,
            dict,
        ):
            continue

        seat_type = _clean_text(
            variable.get(
                "seatType"
            )
        )

        price = _clean_text(
            variable.get(
                "seatCost"
            )
        )

        status = _clean_text(
            variable.get(
                "seatAvalibility"
            )
        )

        if not seat_type:
            continue

        categories.append(
            CatInfo(
                name=seat_type,
                price=price,
                status=status,
            )
        )

    return categories


# ============================================================
# ROBUST SHOWTIME WALKER
# ============================================================

def _walk_showtime_sections(
    obj: Any,
    venue_name: str,
    venue_code: str,
    combo: Optional[str],
    results: List[ShowInfo],
):
    """
    Recursively walk the BMS JSON.

    We intentionally do not depend on a single exact path such
    as:

        showtimeWidgets
          -> groupList
            -> venueGroup
              -> venue-card
                -> showtimesSections

    BMS changes this structure periodically.

    Instead, whenever we encounter a dictionary containing
    showtimesSections, we process it.
    """

    if isinstance(
        obj,
        list,
    ):

        for item in obj:

            _walk_showtime_sections(
                item,
                venue_name,
                venue_code,
                combo,
                results,
            )

        return

    if not isinstance(
        obj,
        dict,
    ):
        return

    # --------------------------------------------------------
    # Update venue context.
    # --------------------------------------------------------

    additional = obj.get(
        "additionalData",
        {},
    )

    if isinstance(
        additional,
        dict,
    ):

        possible_name = _clean_text(
            additional.get(
                "venueName"
            )
        )

        possible_code = _clean_text(
            additional.get(
                "venueCode"
            )
        )

        if possible_name:

            venue_name = (
                possible_name
            )

        if possible_code:

            venue_code = (
                possible_code
            )

    # --------------------------------------------------------
    # Process showtime sections.
    # --------------------------------------------------------

    sections = obj.get(
        "showtimesSections"
    )

    if isinstance(
        sections,
        list,
    ):

        log.debug(
            "FOUND SHOWTIME SECTIONS | "
            "venue=%s | code=%s | sections=%d",
            venue_name,
            venue_code,
            len(sections),
        )

        for section in sections:

            if not isinstance(
                section,
                dict,
            ):
                continue

            showtimes = section.get(
                "showtimes",
                [],
            )

            if not isinstance(
                showtimes,
                list,
            ):
                continue

            for show in showtimes:

                if not isinstance(
                    show,
                    dict,
                ):
                    continue

                _process_show(
                    show=show,
                    venue_name=venue_name,
                    venue_code=venue_code,
                    combo=combo,
                    results=results,
                )

    # --------------------------------------------------------
    # Continue walking children.
    # --------------------------------------------------------

    for key, value in obj.items():

        if key == "showtimesSections":
            continue

        _walk_showtime_sections(
            value,
            venue_name,
            venue_code,
            combo,
            results,
        )


# ============================================================
# PROCESS ONE SHOW
# ============================================================

def _process_show(
    show: Dict[str, Any],
    venue_name: str,
    venue_code: str,
    combo: Optional[str],
    results: List[ShowInfo],
):

    show_time = _clean_text(
        show.get(
            "title"
        )
    )

    if not show_time:

        additional = show.get(
            "additionalData",
            {},
        )

        if isinstance(
            additional,
            dict,
        ):

            show_time = _clean_text(
                additional.get(
                    "showTime"
                )
                or additional.get(
                    "showtime"
                )
            )

    if not show_time:
        return

    # --------------------------------------------------------
    # FORMAT
    # --------------------------------------------------------

    format_text = (
        _extract_format_text(
            show
        )
    )

    # BMS also puts format in CTA analytics.
    if not format_text:

        cta = show.get(
            "cta",
            {},
        )

        if isinstance(
            cta,
            dict,
        ):

            analytics = cta.get(
                "analytics",
                {},
            )

            if isinstance(
                analytics,
                dict,
            ):

                format_text = _clean_text(
                    analytics.get(
                        "format"
                    )
                )

    # --------------------------------------------------------
    # LANGUAGE / FORMAT
    # --------------------------------------------------------

    show_language, movie_format = (
        _parse_language_format(
            format_text
        )
    )

    # --------------------------------------------------------
    # COMBO FILTER
    # --------------------------------------------------------

    if combo:

        if not _showtime_matches_combo(
            format_text,
            combo,
        ):

            log.debug(
                "SHOW SKIPPED | venue=%s | "
                "time=%s | format=%r | "
                "requested=%r",
                venue_name,
                show_time,
                format_text,
                combo,
            )

            return

    # --------------------------------------------------------
    # SCREEN
    # --------------------------------------------------------

    screen_attr = _clean_text(
        show.get(
            "screenAttr"
        )
    )

    # --------------------------------------------------------
    # CATEGORIES
    # --------------------------------------------------------

    categories = (
        _extract_categories(
            show
        )
    )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    info = ShowInfo(
        venue_name=venue_name,
        venue_code=venue_code,
        time=show_time,
        screen_attr=screen_attr,
        categories=categories,
        language=show_language,
        movie_format=movie_format,
        language_format_text=format_text,
    )

    # Avoid duplicate entries.
    for existing in results:

        if (
            existing.venue_code
            == info.venue_code
            and existing.time
            == info.time
            and existing.language_format_text
            == info.language_format_text
        ):

            return

    results.append(
        info
    )

    log.info(
        "SHOW FOUND | venue=%s | "
        "code=%s | time=%s | format=%s | "
        "screen=%s | categories=%d",
        venue_name,
        venue_code,
        show_time,
        format_text,
        screen_attr,
        len(categories),
    )


# ============================================================
# PARSE SHOWTIMES
# ============================================================

def _parse_showtimes(
    data: Dict[str, Any],
    combo: Optional[str],
) -> List[ShowInfo]:

    results: List[
        ShowInfo
    ] = []

    if not isinstance(
        data,
        dict,
    ):
        return results

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Walk the COMPLETE response instead of relying on one
    # hard-coded JSON path.
    # --------------------------------------------------------

    _walk_showtime_sections(
        data,
        "",
        "",
        combo,
        results,
    )

    log.info(
        "PARSER RESULT | shows=%d | combo=%s",
        len(results),
        combo,
    )

    return results


# ============================================================
# GET SHOWS
# ============================================================

def get_show_infos_for_date(
    event_code: str,
    region_slug: str,
    date,
    combo: Optional[str] = None,
) -> List[ShowInfo]:

    date_code = normalize_date_code(
        date
    )

    original_event_code = (
        _clean_text(
            event_code
        ).upper()
    )

    resolved_event_code = (
        original_event_code
    )

    language = ""
    ref_event_code = (
        original_event_code
    )

    # --------------------------------------------------------
    # Resolve selected language / format.
    # --------------------------------------------------------

    if combo:
     (
        resolved_event_code,
        language,
        ref_event_code,
    ) = resolve_variant(
        event_code=(
            original_event_code
        ),
        region_slug=region_slug,
        combo=combo,
        date_code=date_code,
    )

    normalized_combo = (
        _normalize_combo(
            combo
        )
    )

    log.info(
        "GET SHOWS | original=%s | "
        "resolved=%s | language=%s | "
        "ref=%s | date=%s | combo=%s",
        original_event_code,
        resolved_event_code,
        language,
        ref_event_code,
        date_code,
        normalized_combo,
    )

    # ========================================================
    # REQUEST 1
    # ========================================================

    data = fetch_bms(
        event_code=(
            resolved_event_code
        ),
        region_slug=region_slug,
        date_code=date_code,
        language=language or None,
        ref_event_code=ref_event_code,
    )

    results = _parse_showtimes(
        data,
        normalized_combo,
    )

    if results:

        log.info(
            "DATE RESULT | language request "
            "returned %d shows",
            len(results),
        )

        return results

    # ========================================================
    # REQUEST 2
    #
    # Retry WITHOUT language parameter.
    # ========================================================

    if language:

        log.warning(
            "NO SHOWS WITH LANGUAGE FILTER | "
            "retrying without language"
        )

        fallback_data = fetch_bms(
            event_code=(
                resolved_event_code
            ),
            region_slug=region_slug,
            date_code=date_code,
            language=None,
            ref_event_code=ref_event_code,
        )

        fallback_results = (
            _parse_showtimes(
                fallback_data,
                normalized_combo,
            )
        )

        if fallback_results:

            log.info(
                "LANGUAGE FALLBACK | "
                "shows=%d",
                len(fallback_results),
            )

            return fallback_results

    # ========================================================
    # REQUEST 3
    #
    # If a different variant event was resolved, retry the
    # original event.
    # ========================================================

    if (
        resolved_event_code
        != original_event_code
    ):

        log.warning(
            "NO SHOWS FROM RESOLVED EVENT | "
            "retrying original event"
        )

        original_data = fetch_bms(
            event_code=(
                original_event_code
            ),
            region_slug=region_slug,
            date_code=date_code,
            language=None,
            ref_event_code=(
                original_event_code
            ),
        )

        original_results = (
            _parse_showtimes(
                original_data,
                normalized_combo,
            )
        )

        if original_results:

            log.info(
                "ORIGINAL EVENT FALLBACK | "
                "shows=%d",
                len(original_results),
            )

            return original_results

    log.warning(
        "NO MATCHING SHOWS | "
        "event=%s | resolved=%s | "
        "date=%s | combo=%s",
        original_event_code,
        resolved_event_code,
        date_code,
        normalized_combo,
    )

    return []


# ============================================================
# VENUES
# ============================================================

def get_venues_for_date(
    event_code: str,
    region_slug: str,
    date,
    combo: Optional[str] = None,
) -> List[Venue]:

    shows = get_show_infos_for_date(
        event_code=event_code,
        region_slug=region_slug,
        date=date,
        combo=combo,
    )

    grouped: Dict[
        str,
        Venue,
    ] = {}

    for show in shows:

        if show.venue_name not in grouped:

            grouped[
                show.venue_name
            ] = Venue(
                name=show.venue_name,
                showtimes=[],
            )

        venue = grouped[
            show.venue_name
        ]

        if show.time not in venue.showtimes:

            venue.showtimes.append(
                show.time
            )

    return list(
        grouped.values()
    )


# ============================================================
# DEBUG
# ============================================================

def debug_date(
    event_code: str,
    region_slug: str,
    date,
    combo: Optional[str] = None,
):

    print()
    print("=" * 72)
    print("BOOKMYSHOW DEBUG")
    print("=" * 72)

    print(
        f"Event : {event_code}"
    )

    print(
        f"Region: {region_slug}"
    )

    print(
        f"Date  : "
        f"{normalize_date_code(date)}"
    )

    print(
        f"Combo : {combo}"
    )

    print("=" * 72)
    print()

    shows = get_show_infos_for_date(
        event_code=event_code,
        region_slug=region_slug,
        date=date,
        combo=combo,
    )

    print()
    print(
        f"MATCHING SHOWS: {len(shows)}"
    )
    print()

    for index, show in enumerate(
        shows,
        start=1,
    ):

        print(
            f"{index}. "
            f"{show.venue_name}"
        )

        print(
            f"   Code   : "
            f"{show.venue_code}"
        )

        print(
            f"   Time   : "
            f"{show.time}"
        )

        print(
            f"   Format : "
            f"{show.language_format_text}"
        )

        print(
            f"   Screen : "
            f"{show.screen_attr}"
        )

        for category in show.categories:

            print(
                f"      "
                f"{category.name}"
                f" | {category.price}"
                f" | {category.status}"
            )

        print()

    return shows


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        ),
    )

    EVENT = "ET00473215"

    REGION = "chennai"

    # Friday, 04 September 2026
    DATE = "20260905"

    COMBO = "Malayalam - 2D"

    debug_date(
        event_code=EVENT,
        region_slug=REGION,
        date=DATE,
        combo=COMBO,
    )