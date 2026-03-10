"""Fetch art detail from LitRes REST API (GET /foundation/api/arts/{id}).

Phase 4: used by ingest_book() to get genres, series, and release_date for new books.
The catalog/facets API always returns series:[] and genres:[] — this endpoint fills those gaps.
"""

from datetime import datetime

from app.scrapers.catalog import parse_art
from app.scrapers.client import RateLimitedClient
from app.scrapers.models import ArtDetail, ArtGenreRef, ArtSeriesRef

ARTS_DETAIL_PATH = "/foundation/api/arts/{art_id}"


def fetch_arts_detail(client: RateLimitedClient, art_id: int) -> ArtDetail:
    """
    Fetch full art detail for one book. One API call.
    Returns ArtDetail with base Art + genres + series + release_date.
    Raises httpx.HTTPStatusError on 4xx/5xx.
    """
    path = ARTS_DETAIL_PATH.format(art_id=art_id)
    data = client.get_api(path)
    raw = data["payload"]["data"]

    art = parse_art(raw)

    genres = [
        ArtGenreRef(
            id=g["id"],
            name=g.get("name") or "",
            url=g.get("url") or "",
            is_main=bool(g.get("is_main")),
        )
        for g in (raw.get("genres") or [])
        if g.get("id")
    ]

    series = [
        ArtSeriesRef(
            id=s["id"],
            name=s.get("name") or "",
            url=s.get("url") or "",
            art_order=s.get("art_order"),
            unique_arts_count=s.get("unique_arts_count") or 0,
        )
        for s in (raw.get("series") or [])
        if s.get("id")
    ]

    release_date: datetime | None = None
    rd_str = raw.get("last_released_at")
    if rd_str:
        try:
            release_date = datetime.fromisoformat(rd_str)
        except ValueError:
            pass

    return ArtDetail(art=art, genres=genres, series=series, release_date=release_date)
