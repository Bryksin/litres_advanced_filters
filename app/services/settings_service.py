"""SettingsService — load/save CatalogQuery from UserSettings table.

All values are sanitised both when they come from a request (see the
``parse_*`` helpers used by the catalog controller) and when they are loaded
from the database, so a row saved by an older version with bad values heals
itself on the next read instead of breaking every catalog request.
"""

import json
import math
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import UserSettings
from app.models.catalog_query import CatalogQuery

# Spec F-6: when narrator exclusion is enabled, the list is pre-filled with the
# LitRes AI narrator. The toggle itself is off by default.
DEFAULT_EXCLUDED_NARRATORS: tuple[str, ...] = ("Литрес Авточтец",)

VALID_SORTS: tuple[str, ...] = ("release_date_desc", "rating_avg_desc", "rating_count_desc")
DEFAULT_SORT = "release_date_desc"

# Sanity limits for user-supplied values (Phase 16 audit Q2/Q4/Q5, F6/F7).
NAME_LIST_MAX_ITEMS = 100
NAME_MAX_LEN = 200
NAME_LIST_JSON_MAX_LEN = 50_000
SERIES_SIZE_MAX = 10_000
RATING_COUNT_MAX = 10_000_000
PAGE_MAX = 100_000
RATING_MIN_VALUE = 0.0
RATING_MAX_VALUE = 5.0


# ---------------------------------------------------------------------------
# Value sanitisers (shared by the controller and by get())
# ---------------------------------------------------------------------------


def clean_name_list(value) -> list[str]:
    """Return a safe list of person names from any decoded JSON value.

    Keeps only non-empty strings, collapses whitespace, truncates over-long
    names, drops case-insensitive duplicates (first spelling wins, so the
    user's display spelling is kept) and caps the list length.
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = " ".join(item.split())[:NAME_MAX_LEN].strip()
        if not name:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= NAME_LIST_MAX_ITEMS:
            break
    return out


def normalize_name(name: str) -> str:
    """Comparison key for person names: whitespace-collapsed, casefolded, ё→е.

    SQLite's lower() does not fold Cyrillic, so this is done in Python.
    """
    return " ".join(name.split()).casefold().replace("ё", "е")


def parse_name_list_json(raw: str | None) -> list[str]:
    """Decode a JSON array of names from a request or DB column; never raises."""
    if not raw or len(raw) > NAME_LIST_JSON_MAX_LEN:
        return []
    try:
        return clean_name_list(json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        return []


def clamp_int(value, lo: int, hi: int) -> int | None:
    """Parse an int (str or int); None when missing/invalid, else clamped to [lo, hi]."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        v = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return min(max(v, lo), hi)


def clamp_rating(value) -> float | None:
    """Parse a rating bound; None when missing/invalid/non-finite, else clamped to 0..5."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(v):
        return None
    return min(max(v, RATING_MIN_VALUE), RATING_MAX_VALUE)


def clean_sort(value) -> str:
    return value if value in VALID_SORTS else DEFAULT_SORT


class SettingsService:
    def __init__(self, session: Session, user_id: int) -> None:
        self.session = session
        self.user_id = user_id

    def get(self) -> CatalogQuery:
        row = self.session.get(UserSettings, self.user_id)
        if row is None:
            # Same defaults as reset(): exclusion off, list pre-filled (F-6).
            return CatalogQuery(excluded_narrators=list(DEFAULT_EXCLUDED_NARRATORS))
        if row.excluded_narrators_json is None:
            # Never-saved row (new user): pre-fill the list per F-6.
            excluded_narrators = list(DEFAULT_EXCLUDED_NARRATORS)
        else:
            excluded_narrators = parse_name_list_json(row.excluded_narrators_json)
        return CatalogQuery(
            genre_id=row.genre_id,
            series_only=bool(row.series_only),
            standalones_only=bool(row.standalones_only),
            series_min=clamp_int(row.series_min, 0, SERIES_SIZE_MAX),
            series_max=clamp_int(row.series_max, 0, SERIES_SIZE_MAX),
            full_series_subscription=bool(row.full_series_subscription),
            exclude_authors=bool(row.exclude_authors),
            excluded_authors=parse_name_list_json(row.excluded_authors_json),
            exclude_narrators=bool(row.exclude_narrators),
            excluded_narrators=excluded_narrators,
            rating_min=clamp_rating(row.rating_min),
            rating_max=clamp_rating(row.rating_max),
            rating_count_min=clamp_int(row.rating_count_min, 0, RATING_COUNT_MAX),
            hide_listened=bool(row.hide_listened),
            incomplete_series_only=bool(row.incomplete_series_only),
            sort=clean_sort(row.sort),
        )

    def reset(self) -> CatalogQuery:
        """Reset all filter settings to defaults, return clean query.

        The genre and the sort order are kept: they are navigation state, not filters.
        """
        row = self.session.get(UserSettings, self.user_id)
        if row is None:
            return CatalogQuery(excluded_narrators=list(DEFAULT_EXCLUDED_NARRATORS))
        row.series_only = False
        row.standalones_only = False
        row.series_min = None
        row.series_max = None
        row.full_series_subscription = False
        row.exclude_authors = False
        row.excluded_authors_json = "[]"
        row.exclude_narrators = False
        row.excluded_narrators_json = json.dumps(list(DEFAULT_EXCLUDED_NARRATORS), ensure_ascii=False)
        row.rating_min = None
        row.rating_max = None
        row.rating_count_min = None
        row.hide_listened = False
        row.incomplete_series_only = False
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return CatalogQuery(
            genre_id=row.genre_id,
            excluded_narrators=list(DEFAULT_EXCLUDED_NARRATORS),
            sort=clean_sort(row.sort),
        )

    def save(self, query: CatalogQuery) -> None:
        """Persist the query. Values are sanitised again here so nothing invalid is stored."""
        row = self.session.get(UserSettings, self.user_id)
        if row is None:
            return
        row.genre_id = query.genre_id
        row.series_only = query.series_only
        row.standalones_only = query.standalones_only
        row.series_min = clamp_int(query.series_min, 0, SERIES_SIZE_MAX)
        row.series_max = clamp_int(query.series_max, 0, SERIES_SIZE_MAX)
        row.full_series_subscription = query.full_series_subscription
        row.exclude_authors = query.exclude_authors
        row.excluded_authors_json = json.dumps(clean_name_list(query.excluded_authors), ensure_ascii=False)
        row.exclude_narrators = query.exclude_narrators
        row.excluded_narrators_json = json.dumps(clean_name_list(query.excluded_narrators), ensure_ascii=False)
        row.rating_min = clamp_rating(query.rating_min)
        row.rating_max = clamp_rating(query.rating_max)
        row.rating_count_min = clamp_int(query.rating_count_min, 0, RATING_COUNT_MAX)
        row.hide_listened = query.hide_listened
        row.incomplete_series_only = query.incomplete_series_only
        row.sort = clean_sort(query.sort)
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()

    def save_sort(self, sort: str) -> None:
        """Persist only the sort order (sort changed outside a filter Apply)."""
        row = self.session.get(UserSettings, self.user_id)
        if row is None or row.sort == clean_sort(sort):
            return
        row.sort = clean_sort(sort)
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()
