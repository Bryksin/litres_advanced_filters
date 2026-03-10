"""Typed structures for LitRes scraper (Phase 3). No persistence; used as return types."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# --- Genre tree (from HTML) ---


@dataclass
class GenreNode:
    """One node in the genre/category tree."""

    id: str  # numeric id from URL (e.g. "5077")
    name: str
    slug: str  # e.g. "kosmicheskaya-5077"
    url: str  # path or full URL, e.g. "/genre/kosmicheskaya-5077/"
    children: list["GenreNode"] = field(default_factory=list)
    # Optional: document count if present in UI (e.g. "Genre 130704")
    count: int | None = None


# --- Catalog / facets API (art object) ---


@dataclass
class PersonRef:
    """Author, reader, translator, etc. from API."""

    id: int
    full_name: str
    url: str  # e.g. "/author/slug/"
    role: str  # author, reader, translator, painter, editor, ...


@dataclass
class ArtRating:
    rated_avg: float | None
    rated_total_count: int
    rated_1_count: int = 0
    rated_2_count: int = 0
    rated_3_count: int = 0
    rated_4_count: int = 0
    rated_5_count: int = 0


@dataclass
class ArtPrices:
    final_price: float
    full_price: float
    currency: str
    discount_price: float | None = None
    discount_percent: float | None = None


@dataclass
class SeriesRef:
    """Series reference from API (minimal)."""

    id: int | None = None
    name: str | None = None
    url: str | None = None
    position: int | None = None  # N in "N of M" series
    total: int | None = None  # M in "N of M" series


@dataclass
class Art:
    """One book/art from catalog (facets API) or author arts API."""

    id: int
    title: str
    url: str  # path, e.g. "/audiobook/author-slug/book-slug-123/"
    art_type: int  # 0 text, 1 audiobook
    persons: list[PersonRef]
    rating: ArtRating
    prices: ArtPrices
    is_available_with_subscription: bool
    is_abonement_art: bool
    language_code: str = "ru"
    cover_url: str | None = None
    series: list[SeriesRef] = field(default_factory=list)
    # Optional raw for debugging
    raw: dict[str, Any] | None = None


# --- Book page (from HTML) ---


@dataclass
class BookDetails:
    """Full book details parsed from book page HTML."""

    art_id: int
    title: str
    authors: list[str]  # full names
    author_urls: list[str]  # paths
    narrators: list[str]
    narrator_urls: list[str]
    series_name: str | None
    series_url: str | None  # path; may be None even when series_id is set
    series_id: int | None  # LitRes series id; prefer over URL-parsing
    position_in_series: int | None  # N
    total_in_series: int | None  # M
    rating_avg: float | None
    rating_count: int | None
    is_available_with_subscription: bool
    price_str: str | None  # as shown, e.g. "549 ₽"
    art_type: str  # "audiobook" | "text_book"


# --- Series page (from HTML) ---


@dataclass
class SeriesBookEntry:
    """One book in a series list (from series page HTML)."""

    art_id: int
    title: str
    url: str
    position: int  # 1-based order in series
    authors: list[str]
    narrators: list[str]
    rating_avg: float | None
    rating_count: int | None
    is_available_with_subscription: bool
    price_str: str | None


@dataclass
class SeriesPage:
    """Series page parsed from HTML."""

    series_id: int
    series_name: str
    series_url: str
    books: list[SeriesBookEntry]
    author_names: list[str] = field(default_factory=list)


# --- Author (from API) ---


@dataclass
class AuthorInfo:
    """Author metadata from GET /foundation/api/authors/{id}."""

    id: int
    full_name: str
    url: str  # path
    image_url: str | None
    arts_count: int
    about_author: str | None
    about_author_html: str | None
    text_arts_count: int = 0
    audio_arts_count: int = 0
    series_count: int = 0
    quotes_count: int = 0
    raw: dict[str, Any] | None = None


# --- Catalog result (facets) ---


@dataclass
class CatalogResult:
    """One page of catalog from facets API."""

    books: list[Art]
    total_count: int
    next_offset: int | None  # None if no more pages
    previous_offset: int | None


# --- Arts detail API (GET /foundation/api/arts/{id}) ---


@dataclass
class ArtGenreRef:
    """Genre reference from arts detail API."""

    id: int
    name: str
    url: str
    is_main: bool = False


@dataclass
class ArtSeriesRef:
    """Series reference from arts detail API (richer than SeriesRef from catalog)."""

    id: int
    name: str
    url: str                  # e.g. "/series/slug-874105/"
    art_order: int | None     # 1-based position; None = author collection, not literary series
    unique_arts_count: int    # total books in series (use for Series.book_count)


@dataclass
class ArtDetail:
    """Full arts detail from GET /foundation/api/arts/{id}."""

    art: "Art"
    genres: list[ArtGenreRef]
    series: list[ArtSeriesRef]
    release_date: "datetime | None"
