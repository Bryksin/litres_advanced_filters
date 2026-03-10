"""Fetch and parse LitRes genre tree from HTML (Phase 3.2)."""

import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

from app.scrapers.client import RateLimitedClient
from app.scrapers.models import GenreNode

log = logging.getLogger(__name__)

GENRE_TREE_URL = "/pages/new_genres/"

# Paths we treat as genre/showroom; exclude special pages
GENRE_PATH_RE = re.compile(r"^/(genre|showroom)/[^/]+-\d+/?$")
EXCLUDE_PATH_PREFIXES = ("/drafts", "/selfpublishing", "/collections/", "/showroom/fanfic")


def _parse_slug_id(href: str) -> tuple[str, str] | None:
    """Extract (slug, id) from path like /genre/kosmicheskaya-5077/ or /showroom/knigi-fantastika-5004/."""
    href = href.split("?")[0].rstrip("/")
    if not GENRE_PATH_RE.match(href):
        return None
    for prefix in EXCLUDE_PATH_PREFIXES:
        if href.startswith(prefix):
            return None
    # Last segment is "slug-id"
    segment = href.rstrip("/").split("/")[-1]
    if "-" not in segment:
        return None
    # Id is the last numeric part (after last hyphen)
    parts = segment.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return (segment, parts[1])


def _parse_name_count(text: str) -> tuple[str, int | None]:
    """
    Split link text into (name, count).

    Handles two formats from LitRes:
      - Main genre tree page:  "GenreName130766"  (compact, no spaces)
      - Sub-genre pages:       "Genre Name9 225 книг[и/а]"  (expanded with suffix)
    """
    text = (text or "").strip()
    if not text:
        return ("", None)
    # Sub-genre page format: "Name 1 234 книг[и/а]" or "Name1234 книги"
    m = re.match(r"^(.+?)\s*([\d][\d\s]*)\s*книг[иа]?\.?\s*$", text)
    if m:
        name = m.group(1).strip()
        count = int(re.sub(r"\s", "", m.group(2)))
        return (name, count)
    # Main page format: trailing digits with no suffix
    m = re.match(r"^(.+?)(\d+)$", text)
    if m:
        return (m.group(1).strip(), int(m.group(2)))
    return (text, None)


def fetch_genre_tree(client: RateLimitedClient) -> list[GenreNode]:
    """
    Fetch the genre tree page and return a flat list of all genre nodes.
    Each node has id, name, slug, url, count; children are not nested (flat list).
    """
    html = client.get_html(GENRE_TREE_URL)
    soup = BeautifulSoup(html, "html.parser")
    seen_urls: set[str] = set()
    nodes: list[GenreNode] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href.startswith("/genre/") and not href.startswith("/showroom/"):
            continue
        href = href.split("?")[0].rstrip("/") + "/"
        if href in seen_urls:
            continue
        parsed = _parse_slug_id(href)
        if not parsed:
            continue
        slug, id_ = parsed
        name, count = _parse_name_count(a.get_text())
        if not name:
            name = slug
        seen_urls.add(href)
        nodes.append(
            GenreNode(
                id=id_,
                name=name,
                slug=slug,
                url=href,
                count=count,
                children=[],
            )
        )

    return nodes


def _parse_subgenres(html: str, parent_url: str, seen_ids: set[str]) -> list[GenreNode]:
    """
    Parse sub-genres from a genre/showroom page HTML.

    LitRes genre pages list their sub-genres in a sidebar block structured as:
      <div> (container)
        <div> <a href="/genre/child-slug-id/">Child Name123 книги</a> </div>
        <div> <a href="/genre/..."> ... </a> </div>
        ...

    Detection: find a <div> whose every direct-child <div> contains exactly one
    valid genre <a> link. This avoids relying on obfuscated CSS class names.
    """
    soup = BeautifulSoup(html, "html.parser")
    parent_href = parent_url.split("?")[0].rstrip("/") + "/"

    for container in soup.find_all("div"):
        direct = [c for c in container.children if getattr(c, "name", None)]
        if len(direct) < 2:
            continue
        if not all(c.name == "div" for c in direct):
            continue

        child_links: list[tuple[str, str, str, str, int | None]] = []
        ok = True
        for wrapper in direct:
            anchors = [
                a
                for a in wrapper.find_all("a", href=True)
                if _parse_slug_id(a.get("href", "").split("?")[0].rstrip("/") + "/") is not None
            ]
            if len(anchors) != 1:
                ok = False
                break
            a = anchors[0]
            href = a.get("href", "").split("?")[0].rstrip("/") + "/"
            if href == parent_href:
                ok = False
                break
            parsed = _parse_slug_id(href)
            if not parsed:
                ok = False
                break
            slug, id_ = parsed
            name, count = _parse_name_count(a.get_text())
            if not name:
                name = slug
            child_links.append((href, name, id_, slug, count))

        if not ok or len(child_links) < 2:
            continue

        # Reject containers where all links point to the same URL (breadcrumb false positive)
        unique_hrefs = {href for href, *_ in child_links}
        if len(unique_hrefs) < 2:
            continue

        # Found the sub-genre block — build nodes, skip already-known ids
        nodes: list[GenreNode] = []
        for href, name, id_, slug, count in child_links:
            if id_ in seen_ids:
                continue
            seen_ids.add(id_)
            nodes.append(
                GenreNode(id=id_, name=name, slug=slug, url=href, count=count, children=[])
            )
        return nodes

    return []


# Retry config for sub-genre page fetches (lighter than bulk sync — shorter waits)
_SUBGENRE_RETRY_WAITS = [10, 30]


def _fetch_subgenre_page(client: RateLimitedClient, url: str, name: str) -> str | None:
    """Fetch a single sub-genre page with retry on 5xx errors.

    Returns HTML string on success, None if all attempts fail.
    """
    attempts = [None] + _SUBGENRE_RETRY_WAITS  # first attempt + retries
    for i, wait in enumerate(attempts):
        if wait is not None:
            log.info("Retrying %s in %ds (attempt %d/%d)...", name, wait, i + 1, len(_SUBGENRE_RETRY_WAITS))
            time.sleep(wait)
        try:
            return client.get_html(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                log.warning(
                    "HTTP %d fetching sub-genres for %s (attempt %d/%d)",
                    exc.response.status_code, url, i + 1, len(attempts),
                )
                continue
            log.warning("HTTP %d fetching sub-genres for %s — not retryable", exc.response.status_code, url)
            return None
        except Exception as exc:
            log.warning("Non-HTTP error fetching sub-genres for %s: %s", url, exc)
            return None

    log.error("All %d attempts failed for sub-genre page %s — skipping", len(attempts), url)
    return None


def fetch_genre_tree_hierarchical(client: RateLimitedClient) -> list[GenreNode]:
    """
    Fetch the full 3-level genre tree and return root nodes with children populated.

    Level 1: root sections from /pages/new_genres/  (e.g. Легкое чтение)
    Level 2: direct children from the same page      (e.g. Фантастика)
    Level 3: grandchildren fetched from each level-2 page (e.g. Космическая фантастика)

    LitRes /pages/new_genres/ HTML structure (as of 2026-02):
      Each section is a <div> with exactly two direct element children:
        1. <a>  — the root genre link
        2. <div> — wrapper containing all level-2 genre <a> links (root repeated as first)
      Detected structurally — no reliance on obfuscated CSS class names.
    """
    html = client.get_html(GENRE_TREE_URL)
    soup = BeautifulSoup(html, "html.parser")

    roots: list[GenreNode] = []
    seen_ids: set[str] = set()

    for section in soup.find_all("div"):
        # Section pattern: exactly 1 direct <a> + 1 direct <div>
        direct_elements = [c for c in section.children if getattr(c, "name", None)]
        if len(direct_elements) != 2:
            continue
        root_a, children_wrapper = direct_elements
        if root_a.name != "a" or children_wrapper.name != "div":
            continue

        root_href = root_a.get("href", "").split("?")[0].rstrip("/") + "/"
        root_parsed = _parse_slug_id(root_href)
        if not root_parsed:
            continue

        root_slug, root_id = root_parsed
        if root_id in seen_ids:
            continue

        root_name, root_count = _parse_name_count(root_a.get_text())
        if not root_name:
            root_name = root_slug

        # Level-2 children from the main genre tree page
        children: list[GenreNode] = []
        child_seen: set[str] = set()

        for a in children_wrapper.find_all("a", href=True):
            href = a.get("href", "").split("?")[0].rstrip("/") + "/"
            if href == root_href or href in child_seen:
                continue
            parsed = _parse_slug_id(href)
            if not parsed:
                continue
            slug, id_ = parsed
            if id_ in seen_ids:
                continue
            name, count = _parse_name_count(a.get_text())
            if not name:
                name = slug
            child_seen.add(href)
            seen_ids.add(id_)
            children.append(
                GenreNode(id=id_, name=name, slug=slug, url=href, count=count, children=[])
            )

        seen_ids.add(root_id)
        roots.append(
            GenreNode(
                id=root_id,
                name=root_name,
                slug=root_slug,
                url=root_href,
                count=root_count,
                children=children,
            )
        )

    # Level-3: fetch each level-2 genre page to collect grandchildren
    total_l2 = sum(len(r.children) for r in roots)
    log.info("Fetching level-3 sub-genres for %d level-2 genres...", total_l2)

    fetched = 0
    failed_subtrees: list[str] = []
    for root in roots:
        for child in root.children:
            fetched += 1
            log.info("[%d/%d] %s", fetched, total_l2, child.name)
            page_html = _fetch_subgenre_page(client, child.url, child.name)
            if page_html is not None:
                child.children = _parse_subgenres(page_html, child.url, seen_ids)
            else:
                failed_subtrees.append(f"{child.name} ({child.url})")

    if failed_subtrees:
        log.error(
            "%d sub-genre page(s) failed after retries: %s",
            len(failed_subtrees),
            ", ".join(failed_subtrees),
        )

    return roots
