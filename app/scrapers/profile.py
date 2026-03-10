# app/scrapers/profile.py
"""Fetch user's finished (listened) books from LitRes API."""

import logging
from urllib.parse import parse_qs, urlparse

import httpx

from app.scrapers.client import BASE_API, DEFAULT_HEADERS

log = logging.getLogger(__name__)

FINISHED_BOOKS_PATH = "/foundation/api/users/me/arts/finished"
# LitRes enforces limit <= 100 per page.
# Pagination is cursor-based: response includes payload.pagination.next_page
# with an `after` param (base64 cursor). Offset param is ignored.
FINISHED_BOOKS_LIMIT = 100
# Safety cap to avoid infinite loops on API changes
MAX_PAGES = 50


class LitresProfileError(Exception):
    """Raised when fetching profile data fails."""


def fetch_finished_book_ids(
    client: httpx.Client,
    access_token: str,
) -> list[int]:
    """Fetch all book IDs the user has marked as finished/listened.

    Uses cursor-based pagination (payload.pagination.next_page) to
    retrieve all finished books across multiple requests.

    Args:
        client: An httpx.Client instance.
        access_token: JWT from litres_login().

    Returns:
        List of book IDs (art_id) that are finished.

    Raises:
        LitresProfileError: On auth failure or network error.
    """
    headers = {
        **DEFAULT_HEADERS,
        "authorization": f"Bearer {access_token}",
    }

    all_book_ids: list[int] = []
    after_cursor: str | None = None
    page = 0

    while page < MAX_PAGES:
        page += 1
        params: dict[str, str | int] = {"limit": FINISHED_BOOKS_LIMIT}
        if after_cursor:
            params["after"] = after_cursor

        url = BASE_API + FINISHED_BOOKS_PATH

        try:
            resp = client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise LitresProfileError(f"Network error fetching finished books: {exc}") from exc

        if resp.status_code == 401:
            raise LitresProfileError("Token expired or invalid (HTTP 401)")
        if resp.status_code >= 400:
            body = resp.text[:500]
            log.error("Finished books API error: HTTP %d — %s", resp.status_code, body)
            raise LitresProfileError(f"Failed to fetch finished books: HTTP {resp.status_code}")

        data = resp.json()
        payload = data.get("payload") or {}
        items = payload.get("data") or []

        page_ids = [item["id"] for item in items if "id" in item]
        all_book_ids.extend(page_ids)
        log.info("Finished books page %d: %d items (total so far: %d)", page, len(page_ids), len(all_book_ids))

        # Check for next page cursor
        pagination = payload.get("pagination") or {}
        next_page = pagination.get("next_page")
        if not next_page or not page_ids:
            break

        # Extract `after` cursor from next_page URL
        parsed = urlparse(next_page)
        qs = parse_qs(parsed.query)
        after_values = qs.get("after", [])
        if not after_values:
            break
        after_cursor = after_values[0]

    log.info("Fetched %d finished book IDs from LitRes (%d pages)", len(all_book_ids), page)
    return all_book_ids
