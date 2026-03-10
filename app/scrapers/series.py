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
    text = soup.get_text(" ", strip=True)

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
            if "Сергей" in block_text or "Лукьяненко" in block_text:
                for auth_a in block.find_all("a", href=re.compile(r"^/author/")):
                    name = auth_a.get_text(strip=True)
                    if name and name not in authors:
                        authors.append(name)
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


def fetch_series_page(
    client: RateLimitedClient,
    path_or_url: str,
    *,
    art_types: str = "audiobook",
    languages: str = "ru",
) -> SeriesPage | None:
    """
    Fetch series page HTML and parse. path_or_url: e.g. /series/slug-789982/ or full URL.
    art_types and languages are query params for filtering (no subscription filter — intentional,
    required to ingest all series members for F-10 correctness).
    """
    sep = "&" if "?" in path_or_url else "?"
    path_or_url = f"{path_or_url}{sep}art_types={art_types}&languages={languages}"
    html = client.get_html(path_or_url)
    return parse_series_page(html, series_url=path_or_url)
