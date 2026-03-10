"""CatalogQuery — typed filter/sort state for catalog queries.

Fields match UserSettings columns 1:1. No format/language/subscription —
those are baked into the sync scope.
"""

from dataclasses import dataclass, field


@dataclass
class CatalogQuery:
    genre_id: str | None = None

    # Series vs standalone
    series_only: bool = False
    standalones_only: bool = False
    series_min: int | None = None
    series_max: int | None = None
    full_series_subscription: bool = False

    # Exclusions
    exclude_authors: bool = False
    excluded_authors: list[str] = field(default_factory=list)
    exclude_narrators: bool = False
    excluded_narrators: list[str] = field(default_factory=list)

    # Rating
    rating_min: float | None = None
    rating_max: float | None = None

    # Listened filters (require profile sync)
    hide_listened: bool = False
    incomplete_series_only: bool = False

    # Sort + pagination
    sort: str = "release_date_desc"  # release_date_desc | rating_avg_desc | rating_count_desc
    page: int = 1
    page_size: int = 48
