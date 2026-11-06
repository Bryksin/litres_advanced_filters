"""Template rendering tests for the Phase 16 front-end fixes (F2, F3, F4, F12).

Renders catalog.html / admin dashboard with a hand-built context inside a
request context, so no database or network is involved.
"""

import json
import re
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from flask import render_template

SETTING_DEFAULTS = dict(
    genre_id="5077", series_only=False, standalones_only=False,
    exclude_narrators=False, excluded_narrators=[], exclude_authors=False,
    excluded_authors=[], rating_min=None, rating_max=None, rating_count_min=None,
    series_min=2, series_max=None, full_series_subscription=False,
    hide_listened=False, incomplete_series_only=False, sort="release_date_desc",
)

GENRES = [
    SimpleNamespace(id="1", name="Root", children=[
        SimpleNamespace(id="10", name="Open parent", children=[
            SimpleNamespace(id="5077", name="Leaf", children=[]),
        ]),
        SimpleNamespace(id="11", name="Closed parent", children=[
            SimpleNamespace(id="111", name="Other leaf", children=[]),
        ]),
    ]),
]


@pytest.fixture(scope="module")
def app():
    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _render(app, url="/?genre_id=5077", template="catalog.html", **overrides):
    settings = SimpleNamespace(**{**SETTING_DEFAULTS, **overrides.pop("settings", {})})
    ctx = dict(
        items=[], genres=GENRES, settings=settings, selected_genre_id="5077",
        open_genre_ids={"1", "10"}, current_page=1, has_next=False, total_count=0,
        ignored_count=0, is_offline=False, is_authenticated=False, auth_email=None,
        listened_count=0,
    )
    ctx.update(overrides)
    with app.test_request_context(url):
        return render_template(template, **ctx)


class _Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.by_id[d["id"]] = (tag, d)


def _parse(html):
    p = _Inputs()
    p.feed(html)
    return p.by_id


def test_excluded_names_json_survives_quotes_and_markup(app):
    """F2: names with quotes / markup round-trip through the hidden input attribute."""
    names = ['A"B', "<img src=x onerror=alert(1)>", "O'Neil", "Литрес Авточтец"]
    html = _render(app, settings=dict(exclude_narrators=True, excluded_narrators=names,
                                      exclude_authors=True, excluded_authors=names))
    ids = _parse(html)
    for field in ("excluded-narrators-json", "excluded-authors-json"):
        tag, attrs = ids[field]
        assert json.loads(attrs["value"]) == names
        # no stray attributes leaked out of the value
        assert set(attrs) == {"type", "id", "name", "value"}


def test_ignore_counter_uses_russian_plurals(app):
    """F3: 21 книга / 22 книги / 25 книг (was "21 книг")."""
    for n, word in ((1, "книга"), (21, "книга"), (22, "книги"), (25, "книг"), (111, "книг")):
        html = _render(app, ignored_count=n)
        assert f"В игноре: {n} {word}<" in html


def test_ignore_section_rendered_hidden_when_empty(app):
    """F14: the section exists (hidden) so an ignore can reveal it without reload."""
    _, attrs = _parse(_render(app, ignored_count=0))["ignore-section"]
    assert "hidden" in attrs
    _, attrs = _parse(_render(app, ignored_count=3))["ignore-section"]
    assert "hidden" not in attrs


def test_page_past_the_end_offers_link_to_first_page(app):
    """F4: items empty but total_count > 0 -> dedicated empty state, link drops page."""
    html = _render(app, url="/?genre_id=5077&page=9999", total_count=3800, current_page=9999)
    assert "На странице 9999 ничего нет" in html
    assert "Выберите жанр" not in html
    link = re.search(r'class="empty-state__action">На первую страницу', html)
    assert link
    href = re.search(r'<a href="([^"]+)" class="empty-state__action">На первую', html).group(1)
    assert "page=" not in href and "genre_id=5077" in href


def test_genre_parent_has_separate_toggle_and_select_buttons(app):
    """F12: chevron toggles (aria-expanded/controls), name button selects."""
    html = _render(app)
    ids = _parse(html)
    assert "<summary" not in html and "<details" not in html
    # open branch
    assert re.search(r'aria-expanded="true" aria-controls="genre-sub-10"', html)
    tag, attrs = ids["genre-sub-10"]
    assert tag == "ul" and "hidden" not in attrs
    # closed branch: list rendered hidden
    assert re.search(r'aria-expanded="false" aria-controls="genre-sub-11"', html)
    tag, attrs = ids["genre-sub-11"]
    assert "hidden" in attrs
    assert re.search(r'class="genre-tree__link genre-tree__link--parent"\s+data-genre-id="11"', html)


def test_no_inline_event_handlers(app):
    """CSP readiness: no on*= attributes in the rendered catalog page."""
    html = _render(app, items=[dict(
        type="book", title="T", url="https://www.litres.ru/x/", cover_url="https://x/c.jpg",
        authors=["A"], narrator="N", rating_avg=None, rating_count=0, book_id=1,
    )], total_count=1, auth_notice="Сессия истекла", is_authenticated=True,
        auth_expired=True, auth_email="a@b.c")
    assert not re.search(r"\son[a-z]+=", html)


def test_filter_form_and_sorts_opt_out_of_form_restore(app):
    """F1: no browser form-state restoration on back/forward."""
    ids = _parse(_render(app))
    for el in ("filter-form", "toolbar-sort", "toolbar-sort-mobile"):
        assert ids[el][1].get("autocomplete") == "off"


def test_unrated_card_shows_dash_but_zero_average_shows_number(app):
    base = dict(type="book", title="T", url="/x/", cover_url=None, authors=["A"],
                narrator=None, rating_count=0, book_id=1)
    html = _render(app, items=[dict(base, rating_avg=None)], total_count=1)
    assert 'book-card__rating-value">—<' in html
    html = _render(app, items=[dict(base, rating_avg=0.0)], total_count=1)
    assert 'book-card__rating-value">0.0<' in html
