"""Fetch catalog (genre arts) via LitRes facets API (Phase 3.3)."""

from app.scrapers.client import RateLimitedClient
from app.scrapers.models import (
    Art,
    ArtPrices,
    ArtRating,
    CatalogResult,
    PersonRef,
    SeriesRef,
)

FACETS_PATH = "/foundation/api/genres/{genre_id}/arts/facets"
DEFAULT_LIMIT = 24


def _parse_person(p: dict) -> PersonRef:
    return PersonRef(
        id=p.get("id", 0),
        full_name=p.get("full_name", ""),
        url=p.get("url", ""),
        role=p.get("role", ""),
    )


def _parse_rating(r: dict | None) -> ArtRating:
    if not r:
        return ArtRating(rated_avg=None, rated_total_count=0)
    return ArtRating(
        rated_avg=r.get("rated_avg"),
        rated_total_count=r.get("rated_total_count", 0),
        rated_1_count=r.get("rated_1_count", 0),
        rated_2_count=r.get("rated_2_count", 0),
        rated_3_count=r.get("rated_3_count", 0),
        rated_4_count=r.get("rated_4_count", 0),
        rated_5_count=r.get("rated_5_count", 0),
    )


def _parse_prices(p: dict | None) -> ArtPrices:
    if not p:
        return ArtPrices(final_price=0.0, full_price=0.0, currency="RUB")
    return ArtPrices(
        final_price=float(p.get("final_price", 0) or 0),
        full_price=float(p.get("full_price", 0) or 0),
        currency=p.get("currency", "RUB"),
        discount_price=float(p["discount_price"]) if p.get("discount_price") is not None else None,
        discount_percent=float(p["discount_percent"]) if p.get("discount_percent") is not None else None,
    )


def _parse_series_list(series_list: list | None) -> list[SeriesRef]:
    if not series_list:
        return []
    out = []
    for s in series_list:
        if isinstance(s, dict):
            out.append(
                SeriesRef(
                    id=s.get("id"),
                    name=s.get("name"),
                    url=s.get("url"),
                    position=s.get("position"),
                    total=s.get("total"),
                )
            )
    return out


def parse_art(raw: dict) -> Art:
    """Convert one art object from API payload to Art."""
    persons = [_parse_person(p) for p in raw.get("persons") or []]
    cover = raw.get("cover_url")
    if cover and cover.startswith("/"):
        cover = "https://www.litres.ru" + cover
    return Art(
        id=raw.get("id", 0),
        title=raw.get("title", ""),
        url=raw.get("url", ""),
        art_type=raw.get("art_type", 0),
        persons=persons,
        rating=_parse_rating(raw.get("rating")),
        prices=_parse_prices(raw.get("prices")),
        is_available_with_subscription=bool(raw.get("is_available_with_subscription")),
        is_abonement_art=bool(raw.get("is_abonement_art")),
        language_code=raw.get("language_code", "ru"),
        cover_url=cover,
        series=_parse_series_list(raw.get("series")),
        raw=raw,
    )


def fetch_catalog_page(
    client: RateLimitedClient,
    genre_id: str | int,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    art_types: str = "audiobook",
    languages: str = "ru",
    only_litres_subscription_arts: bool = False,
    only_abonement_arts: bool = False,
    only_high_rated: bool = False,
    show_unavailable: bool = False,
    o: str = "popular",
) -> CatalogResult:
    """
    Fetch one page of catalog from facets API.
    Returns CatalogResult with books, total_count, next_offset, previous_offset.
    """
    path = FACETS_PATH.format(genre_id=str(genre_id))
    params = {
        "art_types": art_types,
        "languages": languages,
        "limit": limit,
        "offset": offset,
        "o": o,
        "show_unavailable": "true" if show_unavailable else "false",
        "is_for_pda": "false",
    }
    if only_litres_subscription_arts:
        params["only_litres_subscription_arts"] = "true"
    if only_abonement_arts:
        params["only_abonement_arts"] = "true"
    if only_high_rated:
        params["only_high_rated"] = "true"

    data = client.get_api(path, params=params)
    payload = data.get("payload") or {}
    items = payload.get("data") or []
    counters = payload.get("counters") or {}
    pagination = payload.get("pagination") or {}

    total = int(counters.get("all", 0))
    books = [parse_art(obj) for obj in items]

    next_page = pagination.get("next_page")
    prev_page = pagination.get("previous_page")
    next_offset = None
    if next_page and "offset=" in next_page:
        try:
            next_offset = int(next_page.split("offset=")[-1].split("&")[0])
        except (IndexError, ValueError):
            pass
    if next_offset is None and len(books) == limit and offset + limit < total:
        next_offset = offset + limit

    previous_offset = None
    if prev_page and "offset=" in prev_page:
        try:
            previous_offset = int(prev_page.split("offset=")[-1].split("&")[0])
        except (IndexError, ValueError):
            pass
    if previous_offset is None and offset > 0:
        previous_offset = max(0, offset - limit)

    return CatalogResult(
        books=books,
        total_count=total,
        next_offset=next_offset,
        previous_offset=previous_offset,
    )


def fetch_catalog(
    client: RateLimitedClient,
    genre_id: str | int,
    *,
    max_pages: int | None = None,
    limit: int = DEFAULT_LIMIT,
    **kwargs: object,
) -> list[Art]:
    """
    Fetch all pages of catalog (facets API) for the given genre and params.
    Yields or returns a single list of all BookCards. Stops at max_pages if set.
    """
    all_books: list[Art] = []
    offset = 0
    pages = 0
    while True:
        page = fetch_catalog_page(
            client,
            genre_id,
            offset=offset,
            limit=limit,
            **kwargs,
        )
        all_books.extend(page.books)
        pages += 1
        if max_pages is not None and pages >= max_pages:
            break
        if page.next_offset is None or not page.books:
            break
        offset = page.next_offset
    return all_books
