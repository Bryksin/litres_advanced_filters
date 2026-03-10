"""Fetch and parse single book page from LitRes HTML (Phase 3.4).

LitRes is a Next.js application — the page content is SSR-embedded as JSON
in a <script id="__NEXT_DATA__"> tag, not rendered as plain HTML text.
Series data, rating, persons etc. are extracted from that JSON blob.
"""

import json
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.client import RateLimitedClient
from app.scrapers.models import BookDetails


def parse_book_page(html: str, base_url: str = "https://www.litres.ru") -> BookDetails | None:
    """Parse book page HTML into BookDetails.

    LitRes uses Next.js SSR: all page data lives in a <script id="__NEXT_DATA__">
    JSON blob under props.pageProps.initialState (itself a JSON string) inside
    rtkqApi.queries["getArtData({...})"]. Plain-text regex approaches do not work
    because the visible text is JS i18n template strings, not real values.

    Returns None if the JSON cannot be found or parsed.
    """
    soup = BeautifulSoup(html, "html.parser")

    next_data_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not next_data_tag or not next_data_tag.string:
        return None

    try:
        next_data = json.loads(next_data_tag.string)
        pp = next_data.get("props", {}).get("pageProps", {})
        raw_state = pp.get("initialState")
        if not raw_state:
            return None
        state = json.loads(raw_state)
        queries = state.get("rtkqApi", {}).get("queries", {})
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None

    # Find the getArtData entry. RTK Query wraps the response as:
    # {"status": "fulfilled", "data": { <art object> }, ...}
    # The art object is the same shape as the catalog API response.
    art: dict | None = None
    for q_key, q_val in queries.items():
        if q_key.startswith("getArtData(") and isinstance(q_val, dict):
            candidate = q_val.get("data")
            if isinstance(candidate, dict) and candidate.get("id"):
                art = candidate
                break

    if art is None:
        return None

    art_id = art.get("id")
    if not art_id:
        return None

    # Persons
    persons = art.get("persons") or []
    authors = [p["full_name"] for p in persons if p.get("role") == "author"]
    author_urls = [p.get("url", "") for p in persons if p.get("role") == "author"]
    narrators = [p["full_name"] for p in persons if p.get("role") == "reader"]
    narrator_urls = [p.get("url", "") for p in persons if p.get("role") == "reader"]

    # Series — first entry only (a book can technically appear in multiple, we use the primary one)
    series_name: str | None = None
    series_url: str | None = None
    series_id: int | None = None
    position_in_series: int | None = None
    total_in_series: int | None = None
    series_list = art.get("series") or []
    if series_list:
        s = series_list[0]
        series_id = s.get("id")  # always prefer numeric id over URL-parsing
        series_name = s.get("name")
        raw_url = s.get("url")
        if raw_url:
            series_url = urljoin(base_url, raw_url)
        position_in_series = s.get("art_order")
        total_in_series = s.get("arts_count")

    # Rating
    rating_data = art.get("rating") or {}
    rating_avg = rating_data.get("rated_avg")
    rating_count = rating_data.get("rated_total_count")

    # Subscription
    is_available_with_subscription = bool(art.get("is_available_with_subscription"))

    # Price
    prices = art.get("prices") or {}
    price_val = prices.get("final_price")
    price_str = f"{int(price_val)} ₽" if price_val else None

    # Art type (0 = text, 1 = audiobook — same as catalog API)
    art_type_int = art.get("art_type", 0)
    art_type = "audiobook" if art_type_int == 1 else "text_book"

    return BookDetails(
        art_id=art_id,
        title=art.get("title") or "Unknown",
        authors=authors,
        author_urls=author_urls,
        narrators=narrators,
        narrator_urls=narrator_urls,
        series_name=series_name,
        series_url=series_url,
        series_id=series_id,
        position_in_series=position_in_series,
        total_in_series=total_in_series,
        rating_avg=rating_avg,
        rating_count=rating_count,
        is_available_with_subscription=is_available_with_subscription,
        price_str=price_str,
        art_type=art_type,
    )


def fetch_book_page(
    client: RateLimitedClient,
    path_or_url: str,
) -> BookDetails | None:
    """Fetch book page HTML and parse to BookDetails.

    path_or_url: e.g. /audiobook/author/book-123/ or full URL.
    """
    html = client.get_html(path_or_url)
    return parse_book_page(html)
