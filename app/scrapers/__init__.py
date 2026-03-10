"""LitRes scraping library (Phase 3). Rate-limited fetchers, no persistence."""

from app.scrapers.author import (
    fetch_author,
    fetch_author_arts,
    fetch_author_arts_page,
)
from app.scrapers.book import fetch_book_page, parse_book_page
from app.scrapers.catalog import (
    fetch_catalog,
    fetch_catalog_page,
)
from app.scrapers.client import RateLimitedClient
from app.scrapers.genres import (
    fetch_genre_tree,
    fetch_genre_tree_hierarchical,
)
from app.scrapers.models import (
    Art,
    AuthorInfo,
    BookDetails,
    CatalogResult,
    GenreNode,
    SeriesPage,
    SeriesBookEntry,
)
from app.scrapers.auth import litres_login, LitresAuthError
from app.scrapers.profile import fetch_finished_book_ids, LitresProfileError
from app.scrapers.series import fetch_series_page, parse_series_page

__all__ = [
    "RateLimitedClient",
    "fetch_genre_tree",
    "fetch_genre_tree_hierarchical",
    "fetch_catalog_page",
    "fetch_catalog",
    "fetch_book_page",
    "parse_book_page",
    "fetch_series_page",
    "parse_series_page",
    "fetch_author",
    "fetch_author_arts_page",
    "fetch_author_arts",
    "GenreNode",
    "Art",
    "BookDetails",
    "CatalogResult",
    "SeriesPage",
    "SeriesBookEntry",
    "AuthorInfo",
    "litres_login",
    "LitresAuthError",
    "fetch_finished_book_ids",
    "LitresProfileError",
]
