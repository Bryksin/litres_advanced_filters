"""Fetch author data via LitRes API only (Phase 3.6)."""

from app.scrapers.catalog import parse_art
from app.scrapers.client import RateLimitedClient
from app.scrapers.models import Art, AuthorInfo

AUTHOR_BY_ID_PATH = "/foundation/api/authors/{author_id}"
AUTHOR_ARTS_PATH = "/foundation/api/authors/{author_slug}/arts"
ARTS_LIMIT = 24


def fetch_author(
    client: RateLimitedClient,
    author_id: int | str,
) -> AuthorInfo | None:
    """
    Fetch author metadata by numeric id. Returns AuthorInfo or None if not found.
    """
    path = AUTHOR_BY_ID_PATH.format(author_id=str(author_id))
    try:
        data = client.get_api(path)
    except Exception:
        return None
    payload = data.get("payload") or {}
    d = payload.get("data")
    if not isinstance(d, dict):
        return None
    return AuthorInfo(
        id=d.get("id", 0),
        full_name=d.get("full_name", ""),
        url=d.get("url", ""),
        image_url=d.get("image_url"),
        arts_count=d.get("arts_count", 0),
        about_author=d.get("about_author"),
        about_author_html=d.get("about_author_html"),
        text_arts_count=d.get("text_arts_count", 0),
        audio_arts_count=d.get("audio_arts_count", 0),
        series_count=d.get("series_count", 0),
        quotes_count=d.get("quotes_count", 0),
        raw=d,
    )


def fetch_author_arts_page(
    client: RateLimitedClient,
    author_slug: str,
    *,
    offset: int = 0,
    limit: int = ARTS_LIMIT,
    show_unavailable: bool = False,
) -> list[Art]:
    """
    Fetch one page of author's arts by slug. Returns list of BookCard.
    """
    path = AUTHOR_ARTS_PATH.format(author_slug=author_slug)
    params = {
        "limit": limit,
        "offset": offset,
        "o": "popular",
        "show_unavailable": "true" if show_unavailable else "false",
    }
    data = client.get_api(path, params=params)
    payload = data.get("payload") or {}
    items = payload.get("data") or []
    return [parse_art(obj) for obj in items]


def fetch_author_arts(
    client: RateLimitedClient,
    author_slug: str,
    *,
    max_pages: int | None = None,
    limit: int = ARTS_LIMIT,
    show_unavailable: bool = False,
) -> list[Art]:
    """
    Fetch all pages of author's arts. Returns combined list of Art.
    """
    all_books: list[Art] = []
    offset = 0
    pages = 0
    while True:
        page_books = fetch_author_arts_page(
            client,
            author_slug,
            offset=offset,
            limit=limit,
            show_unavailable=show_unavailable,
        )
        all_books.extend(page_books)
        pages += 1
        if max_pages is not None and pages >= max_pages:
            break
        if len(page_books) < limit:
            break
        offset += limit
    return all_books
