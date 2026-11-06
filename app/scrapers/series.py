"""Fetch and parse series page from LitRes HTML (Phase 3.5)."""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.client import RateLimitedClient
from app.scrapers.models import SeriesBookEntry, SeriesPage

# Series URL pattern: /series/slug-id/
SERIES_ID_FROM_PATH = re.compile(r"/series/[^/]+-(\d+)/?")
ART_ID_FROM_PATH = re.compile(r"/audiobook/[^/]+/[^/]+-(\d+)/?")
RATING_PATTERN = re.compile(r"(\d+[,.]?\d*)\s*(?:на основе\s+)?(\d+)\s*оценок", re.I)
PRICE_PATTERN = re.compile(r"(\d[\d\s]*)\s*₽")
SUBSCRIPTION_MARKERS = ("по подписке", "или по подписке")


def _extract_art_id(href: str) -> int | None:
    m = ART_ID_FROM_PATH.search(href)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _normalize_rating(s: str) -> float | None:
    if not s:
        return None
    s = s.strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_series_page(
    html: str,
    series_url: str = "",
    base_url: str = "https://www.litres.ru",
) -> SeriesPage | None:
    """
    Parse series page HTML into SeriesPage. Extracts series id/name from URL and content,
    and list of books (title, url, art_id, position, authors, narrators, rating, subscription, price).
    """
    soup = BeautifulSoup(html, "html.parser")
    soup.get_text(" ", strip=True)

    # Series id from URL
    series_id = 0
    m = SERIES_ID_FROM_PATH.search(series_url)
    if m:
        try:
            series_id = int(m.group(1))
        except ValueError:
            pass

    # Series name: from first h1 or title
    series_name = ""
    h1 = soup.find("h1")
    if h1:
        series_name = h1.get_text(strip=True)
    if not series_name:
        series_name = "Series"

    # Normalize series_url
    if series_url and not series_url.startswith("http"):
        series_url = urljoin(base_url, series_url)

    # Collect book links: /audiobook/ or /book/ with art id
    book_entries: list[SeriesBookEntry] = []
    seen_art_ids: set[int] = set()
    position = 0

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/audiobook/" not in href:
            continue
        art_id = _extract_art_id(href)
        if not art_id or art_id in seen_art_ids:
            continue

        # Each book appears multiple times (cover links + text link).
        # Only claim the art_id once we have a non-empty title — otherwise the
        # cover link (empty text) would block the subsequent text link.
        title = a.get_text(strip=True) or ""
        if not title:
            continue

        seen_art_ids.add(art_id)
        position += 1

        # Try to get author/narrator/rating from surrounding context (parent block)
        block = a.parent
        authors: list[str] = []
        narrators: list[str] = []
        rating_avg: float | None = None
        rating_count: int | None = None
        is_subscription = False
        price_str: str | None = None

        for _ in range(5):  # walk up a few levels
            if not block:
                break
            block_text = block.get_text(" ", strip=True)
            for rm in RATING_PATTERN.finditer(block_text):
                rating_avg = _normalize_rating(rm.group(1))
                try:
                    rating_count = int(rm.group(2).replace("\u202f", "").replace(" ", ""))
                except ValueError:
                    pass
                break
            if any(m in block_text.lower() for m in SUBSCRIPTION_MARKERS):
                is_subscription = True
            pm = PRICE_PATTERN.search(block_text)
            if pm:
                price_str = pm.group(0).strip()
            block = getattr(block, "parent", None)

        full_url = href if href.startswith("http") else urljoin(base_url, href)
        book_entries.append(
            SeriesBookEntry(
                art_id=art_id,
                title=title,
                url=full_url,
                position=position,
                authors=authors,
                narrators=narrators,
                rating_avg=rating_avg,
                rating_count=rating_count,
                is_available_with_subscription=is_subscription,
                price_str=price_str,
            )
        )

    # Author names from page (e.g. in series header)
    author_names: list[str] = []
    for auth_a in soup.find_all("a", href=re.compile(r"^/author/")):
        name = auth_a.get_text(strip=True)
        if name and name not in author_names:
            author_names.append(name)

    return SeriesPage(
        series_id=series_id,
        series_name=series_name,
        series_url=series_url,
        books=book_entries,
        author_names=author_names,
    )


# LitRes series pages list 24 books; a shorter page is the last one.
SERIES_PAGE_SIZE = 24
# Hard stop for roster pagination (30 pages -> 720 books).
SERIES_MAX_PAGES = 30


def _with_query(path_or_url: str, **params: object) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sep = "&" if "?" in path_or_url else "?"
    return f"{path_or_url}{sep}{query}"


def fetch_series_page(
    client: RateLimitedClient,
    path_or_url: str,
    *,
    art_types: str = "audiobook",
    languages: str = "ru",
    max_pages: int = SERIES_MAX_PAGES,
) -> SeriesPage | None:
    """
    Fetch a series roster (all pages) and parse it.

    path_or_url: e.g. /series/slug-789982/ or full URL. art_types and languages
    are query params for filtering (no subscription filter — intentional,
    required to ingest all series members for F-10 correctness).

    LitRes shows 24 books per page and links further pages as ``?page=N``.
    Pages are followed until one adds no new art id, returns fewer than a full
    page, or ``max_pages`` is reached. Positions continue across pages (they
    are page order, not LitRes ``art_order``).

    Raises ValueError for an empty URL (it would fetch the LitRes home page)
    and httpx.HTTPStatusError on 4xx/5xx.
    """
    if not path_or_url or not path_or_url.strip("/"):
        raise ValueError("series URL is empty")
    first_url = _with_query(path_or_url, art_types=art_types, languages=languages)
    html = client.get_html(first_url)
    result = parse_series_page(html, series_url=first_url)
    if result is None:
        return None

    seen = {b.art_id for b in result.books}
    page_no = 1
    last_count = len(result.books)
    while last_count >= SERIES_PAGE_SIZE and page_no < max_pages:
        page_no += 1
        page_html = client.get_html(_with_query(first_url, page=page_no))
        page = parse_series_page(page_html, series_url=first_url)
        new_books = [b for b in (page.books if page else []) if b.art_id not in seen]
        if not new_books:
            break
        for b in new_books:
            seen.add(b.art_id)
            b.position = len(result.books) + 1
            result.books.append(b)
        last_count = len(page.books)
    return result
