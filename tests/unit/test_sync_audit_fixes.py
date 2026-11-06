"""Phase 16 sync-engine audit fixes (docs/status/phase16/audit-sync.md S1-S16, W3).

No network: LitRes is replaced by httpx.MockTransport or patched scrapers.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Book,
    BookGenre,
    BookSeries,
    Genre,
    GenreParent,
    Series,
    SyncConfig,
    SyncRun,
)
from app.scrapers.models import (
    Art,
    ArtDetail,
    ArtPrices,
    ArtRating,
    ArtSeriesRef,
    GenreNode,
    PersonRef,
    SeriesBookEntry,
    SeriesPage,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _art(art_id: int, *, sub: bool = True, title: str | None = None) -> Art:
    return Art(
        id=art_id,
        title=title or f"Book {art_id}",
        url=f"/audiobook/a/book-{art_id}/",
        art_type=1,
        persons=[PersonRef(id=10_000 + art_id, full_name="Author", url="/author/a/", role="author")],
        rating=ArtRating(rated_avg=4.0, rated_total_count=5),
        prices=ArtPrices(final_price=0.0, full_price=0.0, currency="RUB"),
        is_available_with_subscription=sub,
        is_abonement_art=sub,
        language_code="ru",
    )


def _detail(art_id: int, series: list[ArtSeriesRef] | None = None, *, sub: bool = True) -> ArtDetail:
    return ArtDetail(
        art=_art(art_id, sub=sub), genres=[], series=series or [],
        release_date=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _sref(series_id: int, order: int | None, url: str = "/series/s-1/", count: int = 3) -> ArtSeriesRef:
    return ArtSeriesRef(id=series_id, name=f"Series {series_id}", url=url, art_order=order,
                        unique_arts_count=count)


def _roster(series_id: int, art_ids: list[int]) -> SeriesPage:
    return SeriesPage(
        series_id=series_id, series_name="S", series_url=f"/series/s-{series_id}/",
        books=[
            SeriesBookEntry(art_id=a, title=f"B{a}", url=f"/audiobook/a/b-{a}/", position=i + 1,
                            authors=[], narrators=[], rating_avg=None, rating_count=None,
                            is_available_with_subscription=False, price_str=None)
            for i, a in enumerate(art_ids)
        ],
    )


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://www.litres.ru/x/")
    return httpx.HTTPStatusError(str(status), request=req,
                                 response=httpx.Response(status, request=req, headers=headers or {}))


def _factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_config(factory, *, root_genre: bool = True):
    with factory() as s:
        s.add(SyncConfig(id=1, genre_slug="legkoe-chtenie", genre_id=201583, art_type="audiobook",
                         language_code="ru", only_subscription=True))
        if root_genre:
            s.add(Genre(id="201583", name="Легкое чтение", slug="legkoe", url="/genre/legkoe/"))
        s.commit()


def _book(book_id: int, *, sub: bool = True, cached_at: datetime | None = None) -> Book:
    return Book(id=book_id, title=f"B{book_id}", url=f"/audiobook/a/b-{book_id}/", art_type=1,
                is_available_with_subscription=sub, is_abonement_art=sub,
                cached_at=cached_at or datetime.now(UTC))


# ---------------------------------------------------------------------------
# S1 — HTML headers
# ---------------------------------------------------------------------------


class TestHtmlHeaders:
    def _client_with_capture(self):
        from app.scrapers.client import RateLimitedClient

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="<html></html>", json=None) if "api." not in str(
                request.url) else httpx.Response(200, json={"ok": 1})

        c = RateLimitedClient(min_delay_seconds=0, jitter_seconds=0)
        c._client = httpx.Client(transport=httpx.MockTransport(handler))
        return c, seen

    def test_html_request_has_no_api_headers_and_no_accept_language(self):
        c, seen = self._client_with_capture()
        c.get_html("/series/x-1/")
        h = seen[0].headers
        for banned in ("app-id", "accept-version", "client-host", "accept-language", "ui-currency"):
            assert banned not in h, banned
        assert h["accept"].startswith("text/html")
        assert "Firefox" in h["user-agent"]

    def test_api_request_keeps_api_headers(self):
        c, seen = self._client_with_capture()
        c.get_api("/foundation/api/arts/1")
        h = seen[0].headers
        assert h["app-id"] == "115"
        assert h["accept"].startswith("application/json")


# ---------------------------------------------------------------------------
# S1 / S6 / S10 — ingest_series and link_book_series
# ---------------------------------------------------------------------------


class TestSeriesLinking:
    def test_roster_403_keeps_book_and_links_it(self, db_session):
        """S1: the triggering book survives a series-page 403 and is linked."""
        from app.sync.ingest import ingest_book, link_book_series

        detail = _detail(1, [_sref(77, 3)])
        with patch("app.sync.ingest.fetch_series_page", side_effect=_http_error(403)):
            sp = db_session.begin_nested()
            ingest_book(db_session, MagicMock(), detail.art, arts_detail=detail)
            n_series, n_members = link_book_series(db_session, MagicMock(), 1, detail)
            sp.commit()

        assert (n_series, n_members) == (1, 0)
        assert db_session.get(Book, 1) is not None
        series = db_session.get(Series, 77)
        assert series is not None and series.roster_synced_at is None  # retried later
        link = db_session.get(BookSeries, {"book_id": 1, "series_id": 77})
        assert link is not None and link.position_in_series == 3

    def test_empty_series_url_never_fetches(self, db_session):
        from app.sync.ingest import ingest_series

        with patch("app.sync.ingest.fetch_series_page") as fsp:
            assert ingest_series(db_session, MagicMock(), 5, "No URL", "", 4) == 0
        fsp.assert_not_called()
        assert db_session.get(Series, 5) is not None

    def test_members_use_art_order_and_skip_linked(self, db_session):
        """S6 position = LitRes art_order; S10 linked members cost no arts call."""
        from app.sync.ingest import ingest_series

        db_session.add(Series(id=9, name="S", url="/series/s-9/", book_count=3))
        db_session.add(_book(100))
        db_session.add(BookSeries(book_id=100, series_id=9, position_in_series=1))
        db_session.flush()

        calls = []

        def fake_detail(client, art_id):
            calls.append(art_id)
            return _detail(art_id, [_sref(9, {101: 5, 102: 7}[art_id])])

        with patch("app.sync.ingest.fetch_series_page", return_value=_roster(9, [100, 101, 102])), \
             patch("app.sync.ingest.fetch_arts_detail", side_effect=fake_detail):
            linked = ingest_series(db_session, MagicMock(), 9, "S", "/series/s-9/", 3)

        assert calls == [101, 102]
        assert linked == 2
        assert db_session.get(BookSeries, {"book_id": 101, "series_id": 9}).position_in_series == 5
        assert db_session.get(BookSeries, {"book_id": 102, "series_id": 9}).position_in_series == 7
        assert db_session.get(Series, 9).roster_synced_at is not None

    def test_member_failure_leaves_roster_unsynced(self, db_session):
        from app.sync.ingest import ingest_series

        def fake_detail(client, art_id):
            if art_id == 2:
                raise _http_error(504)
            return _detail(art_id, [_sref(3, 1)])

        with patch("app.sync.ingest.fetch_series_page", return_value=_roster(3, [1, 2])), \
             patch("app.sync.ingest.fetch_arts_detail", side_effect=fake_detail):
            assert ingest_series(db_session, MagicMock(), 3, "S", "/series/s-3/", 2) == 1
        assert db_session.get(Series, 3).roster_synced_at is None

    def test_trigger_book_linked_without_extra_arts_call(self, db_session):
        from app.sync.ingest import ingest_book, link_book_series

        detail = _detail(1, [_sref(4, 2)])
        ingest_book(db_session, MagicMock(), detail.art, arts_detail=detail)
        with patch("app.sync.ingest.fetch_series_page", return_value=_roster(4, [1])), \
             patch("app.sync.ingest.fetch_arts_detail") as fad:
            link_book_series(db_session, MagicMock(), 1, detail)
        fad.assert_not_called()
        assert db_session.get(Series, 4).roster_synced_at is not None

    def test_known_series_new_member_requeues_roster_and_updates_count(self, db_session):
        """S4 'touched' queue + S11 book_count refresh."""
        from app.sync.ingest import link_book_series

        db_session.add(Series(id=8, name="S", url="/series/s-8/", book_count=2,
                              roster_synced_at=datetime(2026, 1, 1, tzinfo=UTC)))
        db_session.add(_book(1))
        db_session.flush()
        link_book_series(db_session, MagicMock(), 1, _detail(1, [_sref(8, 3, count=9)]))
        s = db_session.get(Series, 8)
        assert s.book_count == 9
        assert s.roster_synced_at is None

    def test_author_collection_ignored(self, db_session):
        from app.sync.ingest import link_book_series

        db_session.add(_book(1))
        db_session.flush()
        assert link_book_series(db_session, MagicMock(), 1, _detail(1, [_sref(8, None)])) == (0, 0)
        assert db_session.get(Series, 8) is None


# ---------------------------------------------------------------------------
# S4 — roster pagination
# ---------------------------------------------------------------------------


def _series_html(ids: list[int]) -> str:
    links = "".join(f'<div><a href="/audiobook/a/book-{i}/">Book {i}</a></div>' for i in ids)
    return f"<html><h1>S</h1>{links}</html>"


class TestSeriesPagination:
    def test_follows_pages_until_short_page(self):
        from app.scrapers.series import fetch_series_page

        client = MagicMock()
        client.get_html.side_effect = [_series_html(list(range(1, 25))), _series_html(list(range(25, 35)))]
        page = fetch_series_page(client, "/series/s-1/")
        assert [b.art_id for b in page.books] == list(range(1, 35))
        assert [b.position for b in page.books] == list(range(1, 35))
        urls = [c.args[0] for c in client.get_html.call_args_list]
        assert urls == [
            "/series/s-1/?art_types=audiobook&languages=ru",
            "/series/s-1/?art_types=audiobook&languages=ru&page=2",
        ]

    def test_stops_when_page_repeats(self):
        from app.scrapers.series import fetch_series_page

        client = MagicMock()
        full = _series_html(list(range(1, 25)))
        client.get_html.side_effect = [full, full]
        page = fetch_series_page(client, "/series/s-1/")
        assert len(page.books) == 24
        assert client.get_html.call_count == 2

    def test_short_first_page_is_one_request(self):
        from app.scrapers.series import fetch_series_page

        client = MagicMock()
        client.get_html.return_value = _series_html([1, 2, 3])
        assert len(fetch_series_page(client, "/series/s-1/").books) == 3
        assert client.get_html.call_count == 1

    def test_empty_url_raises(self):
        from app.scrapers.series import fetch_series_page

        with pytest.raises(ValueError):
            fetch_series_page(MagicMock(), "")


# ---------------------------------------------------------------------------
# S5 — retry-failed
# ---------------------------------------------------------------------------


class TestRetryFailed:
    def test_restores_series_links_and_isolates_db_errors(self, tmp_path):
        from app.sync import bulk

        factory = _factory()
        _seed_config(factory)
        with factory() as s:
            s.add(_book(5))  # pre-existing row used to provoke an IntegrityError
            s.commit()

        failed = tmp_path / "failed_books_x.jsonl"
        failed.write_text("".join(json.dumps({"book_id": i}) + "\n" for i in (1, 2, 3)))

        details = {1: _detail(1, [_sref(50, 1)]), 2: _detail(2), 3: _detail(3, [_sref(50, 2)])}
        real_ingest = bulk.ingest_book

        def ingest(session, client, art, arts_detail=None, **kw):
            if art.id == 2:  # DB error for entry 2 only
                session.add(_book(5))
                session.flush()
            return real_ingest(session, client, art, arts_detail=arts_detail, **kw)

        args = MagicMock(failed_file=str(failed))
        with patch("app.sync.bulk.SessionLocal", factory), \
             patch("app.sync.bulk.RateLimitedClient"), \
             patch("app.scrapers.arts.fetch_arts_detail", side_effect=lambda c, i: details[i]), \
             patch("app.sync.ingest.fetch_series_page", side_effect=_http_error(403)), \
             patch("app.sync.bulk.ingest_book", side_effect=ingest):
            bulk.run_retry_failed_cli(args)

        with factory() as s:
            assert s.get(Book, 1) and s.get(Book, 3)
            assert s.get(Book, 2) is None
            assert s.get(BookSeries, {"book_id": 1, "series_id": 50}).position_in_series == 1
            assert s.get(BookSeries, {"book_id": 3, "series_id": 50}).position_in_series == 2
            assert s.get(BookGenre, {"book_id": 1, "genre_id": "201583"}) is not None
        remaining = [json.loads(line)["book_id"] for line in failed.read_text().splitlines()]
        assert remaining == [2]


# ---------------------------------------------------------------------------
# W3 / S8 — running-sync guard and zombie reaping
# ---------------------------------------------------------------------------


def _run(session, type_: str, *, started_ago: timedelta, heartbeat_ago: timedelta | None = None):
    now = datetime.now(UTC)
    r = SyncRun(sync_config_id=1, type=type_, status="running", started_at=now - started_ago,
                heartbeat_at=(now - heartbeat_ago) if heartbeat_ago is not None else None)
    session.add(r)
    session.commit()
    return r


class TestRunningGuard:
    def test_live_profile_run_does_not_block_bulk(self, db_session):
        from app.sync.common import check_no_running_sync

        r = _run(db_session, "profile", started_ago=timedelta(minutes=2))
        check_no_running_sync(db_session)  # no raise
        assert db_session.get(SyncRun, r.id).status == "running"

    def test_dead_profile_run_is_reaped(self, db_session):
        from app.sync.common import reap_zombie_sync_runs

        r = _run(db_session, "profile", started_ago=timedelta(minutes=20))
        assert reap_zombie_sync_runs(db_session) == 1
        assert db_session.get(SyncRun, r.id).status == "failed"

    def test_live_bulk_blocks(self, db_session):
        from app.sync.common import check_no_running_sync

        _run(db_session, "bulk", started_ago=timedelta(hours=5), heartbeat_ago=timedelta(minutes=1))
        with pytest.raises(RuntimeError, match="already running"):
            check_no_running_sync(db_session)

    def test_bulk_without_heartbeat_is_reaped(self, db_session):
        from app.sync.common import find_running_catalog_sync

        r = _run(db_session, "delta", started_ago=timedelta(hours=5), heartbeat_ago=timedelta(hours=3))
        assert find_running_catalog_sync(db_session) is None
        assert db_session.get(SyncRun, r.id).status == "failed"

    def test_boot_reaps_everything(self, db_session):
        from app.sync.common import reap_all_running_sync_runs

        _run(db_session, "bulk", started_ago=timedelta(minutes=1), heartbeat_ago=timedelta(seconds=5))
        _run(db_session, "profile", started_ago=timedelta(minutes=1))
        assert reap_all_running_sync_runs(db_session) == 2
        assert db_session.query(SyncRun).filter_by(status="running").count() == 0


# ---------------------------------------------------------------------------
# S15 — log retention
# ---------------------------------------------------------------------------


class TestLogRetention:
    def test_prunes_old_run_files_and_caps_cron_log(self, tmp_path):
        from app.sync.logging_setup import prune_sync_logs

        now = time.time()
        old = now - 20 * 86400
        for name in ("sync_20260101_000000.log", "sync_errors_20260101_000000.log",
                     "failed_books_20260101_000000.jsonl"):
            p = tmp_path / name
            p.write_text("x")
            os.utime(p, (old, old))
        fresh = tmp_path / "sync_20261105_000000.log"
        fresh.write_text("y")
        unrelated = tmp_path / "notes.txt"
        unrelated.write_text("z")
        os.utime(unrelated, (old, old))
        cron = tmp_path / "cron.log"
        cron.write_bytes(b"".join(f"line {i}\n".encode() for i in range(20000)))

        stats = prune_sync_logs(str(tmp_path), keep_days=14, cron_log_max_bytes=50_000,
                                cron_log_keep_bytes=1_000, now=now)

        assert stats["deleted"] == 3
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "cron.log", "cron.log.1", "notes.txt", "sync_20261105_000000.log"]
        assert cron.stat().st_size == 0
        tail = (tmp_path / "cron.log.1").read_bytes()
        assert len(tail) <= 1_000 and tail.endswith(b"line 19999\n") and tail.startswith(b"line ")

    def test_small_cron_log_untouched(self, tmp_path):
        from app.sync.logging_setup import prune_sync_logs

        (tmp_path / "cron.log").write_text("hello\n")
        assert prune_sync_logs(str(tmp_path))["cron_log_truncated"] == 0
        assert (tmp_path / "cron.log").read_text() == "hello\n"

    def test_missing_dir_is_noop(self, tmp_path):
        from app.sync.logging_setup import prune_sync_logs

        assert prune_sync_logs(str(tmp_path / "nope"))["deleted"] == 0


# ---------------------------------------------------------------------------
# S3 — stale-book refresh, S4 — roster refresh order
# ---------------------------------------------------------------------------


class TestMaintenance:
    def test_stale_books_flags_follow_litres(self, db_session):
        from app.sync.refresh import refresh_stale_books

        old = datetime.now(UTC) - timedelta(days=30)
        db_session.add_all([
            _book(1, sub=False, cached_at=old),          # joined subscription
            _book(2, sub=True, cached_at=old),           # left subscription
            _book(3, sub=True, cached_at=old),           # removed from LitRes (404)
            _book(4, sub=True, cached_at=old),           # transient error: untouched
            _book(5, sub=True),                          # fresh: not checked
        ])
        db_session.commit()

        def fake_detail(client, art_id):
            if art_id == 3:
                raise _http_error(404)
            if art_id == 4:
                raise _http_error(503)
            return _detail(art_id, sub=(art_id == 1))

        with patch("app.sync.refresh.fetch_arts_detail", side_effect=fake_detail) as fad:
            stats = refresh_stale_books(db_session, MagicMock(), limit=10)

        assert sorted(c.args[1] for c in fad.call_args_list) == [1, 2, 3, 4]
        assert stats == {"checked": 3, "sub_on": 1, "sub_off": 2, "gone": 1, "failed": 1}
        assert db_session.get(Book, 1).is_available_with_subscription is True
        assert db_session.get(Book, 2).is_available_with_subscription is False
        assert db_session.get(Book, 3).is_available_with_subscription is False
        assert db_session.get(Book, 4).is_available_with_subscription is True
        assert db_session.get(Book, 4).cached_at.replace(tzinfo=UTC) <= old + timedelta(seconds=1)

    def test_roster_refresh_takes_unsynced_first_and_skips_empty_url(self, db_session):
        from app.sync.refresh import refresh_series_rosters

        t = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.add_all([
            Series(id=1, name="old", url="/series/a-1/", book_count=1, roster_synced_at=t),
            Series(id=2, name="never", url="/series/a-2/", book_count=1),
            Series(id=3, name="newer", url="/series/a-3/", book_count=1, roster_synced_at=t + timedelta(days=1)),
            Series(id=4, name="nourl", url="", book_count=1),
        ])
        db_session.commit()
        with patch("app.sync.refresh.ingest_series", return_value=0) as ing:
            refresh_series_rosters(db_session, MagicMock(), limit=2)
        assert [c.args[2] for c in ing.call_args_list] == [2, 1]


# ---------------------------------------------------------------------------
# S2 — genre DAG
# ---------------------------------------------------------------------------


def _node(id_: str, children=None, complete=False) -> GenreNode:
    return GenreNode(id=id_, name=f"G{id_}", slug=f"g-{id_}", url=f"/genre/g-{id_}/",
                     children=children or [], children_complete=complete)


class TestGenreDag:
    def _tree(self):
        hist_adv = _node("5095")
        return [
            _node("201583", [_node("5004", [hist_adv], complete=True)], complete=True),
            _node("201599", [_node("6000", [hist_adv, _node("6001")], complete=True)], complete=True),
        ]

    def test_multi_parent_edges_and_descendants(self, db_session):
        from app.services.genre_service import (
            get_genre_ancestor_ids,
            get_genre_descendant_ids,
            get_genre_tree,
        )
        from app.sync.genres import upsert_genre_tree

        assert upsert_genre_tree(db_session, self._tree()) == 6
        edges = set(db_session.execute(select(GenreParent.genre_id, GenreParent.parent_id)).all())
        assert ("5095", "5004") in edges and ("5095", "6000") in edges
        assert "5095" in get_genre_descendant_ids(db_session, "201583")
        assert "5095" in get_genre_descendant_ids(db_session, "201599")
        assert {"5004", "6000", "201583", "201599"} <= get_genre_ancestor_ids(db_session, "5095")

        db_session.commit()
        db_session.expire_all()
        root = get_genre_tree(db_session)[0]
        assert [c.id for c in root.children[0].children] == ["5095"]
        assert {p.id for p in db_session.get(Genre, "5095").parents} == {"5004", "6000"}

    def test_stale_edge_pruned_only_under_complete_parent(self, db_session):
        from app.sync.genres import upsert_genre_tree

        upsert_genre_tree(db_session, self._tree())
        # Second scrape: 5095 gone from 5004 (complete), 6000's page failed (incomplete).
        tree = [
            _node("201583", [_node("5004", [_node("5010")], complete=True)], complete=True),
            _node("201599", [_node("6000", [], complete=False)], complete=True),
        ]
        upsert_genre_tree(db_session, tree)
        edges = set(db_session.execute(select(GenreParent.genre_id, GenreParent.parent_id)).all())
        assert ("5095", "5004") not in edges
        assert ("5095", "6000") in edges and ("6001", "6000") in edges

    def test_parent_id_only_rows_still_form_tree(self, db_session):
        """Rows written before genre_parent existed (or by tests) keep working."""
        from app.services.genre_service import get_genre_descendant_ids

        db_session.add_all([
            Genre(id="1", name="r", slug="r", url="/r/"),
            Genre(id="2", parent_id="1", name="c", slug="c", url="/c/"),
        ])
        db_session.commit()
        assert get_genre_descendant_ids(db_session, "1") == {"1", "2"}
        assert [c.id for c in db_session.get(Genre, "1").children] == ["2"]

    def test_scraper_keeps_genre_under_every_parent(self):
        from app.scrapers.genres import fetch_genre_tree_hierarchical

        main = (
            "<html><div>"
            '<div><a href="/genre/legkoe-201583/">Легкое1</a>'
            '<div><a href="/genre/legkoe-201583/">Легкое1</a><a href="/genre/prikl-5006/">Прикл2</a></div></div>'
            '<div><a href="/genre/hist-201599/">История1</a>'
            '<div><a href="/genre/hist-201599/">История1</a><a href="/genre/ist-prikl-5095/">ИстПрикл2</a>'
            '<a href="/genre/hist-other-6002/">Другое3</a></div></div>'
            "</div></html>"
        )

        def sub(*links):
            return "<html><div>" + "".join(
                f'<div><a href="/genre/{s}/">{n} 12 книг</a></div>' for s, n in links) + "</div></html>"

        pages = {
            "/pages/new_genres/": main,
            # Приключения lists a level-2 genre of another root (kept) + a leaf.
            "/genre/prikl-5006/": sub(("ist-prikl-5095", "Ист прикл"), ("morskie-5092", "Морские")),
            # 5095's own page lists a same-root sibling (6002, excluded) and the
            # root (excluded) and a real child.
            "/genre/ist-prikl-5095/": sub(("hist-other-6002", "Другое"), ("hist-201599", "История"),
                                          ("piraty-7000", "Пираты")),
            "/genre/hist-other-6002/": sub(("a-1", "A"), ("b-2", "B")),
        }
        client = MagicMock()
        client.get_html.side_effect = lambda url: pages[url]
        roots = fetch_genre_tree_hierarchical(client)
        by_id = {r.id: r for r in roots}
        assert [g.id for g in by_id["201583"].children[0].children] == ["5095", "5092"]
        hist_children = {c.id: c for c in by_id["201599"].children}
        assert [g.id for g in hist_children["5095"].children] == ["7000"]

    def test_cycle_edges_are_dropped(self, db_session):
        from app.sync.genres import upsert_genre_tree

        # A (level 2 under X) lists B; B (level 2 under Y) lists A.
        tree = [
            _node("X", [_node("A", [_node("B")], complete=True)], complete=True),
            _node("Y", [_node("B", [_node("A")], complete=True)], complete=True),
        ]
        upsert_genre_tree(db_session, tree)
        db_session.commit()
        edges = set(db_session.execute(select(GenreParent.genre_id, GenreParent.parent_id)).all())
        assert ("A", "X") in edges and ("B", "Y") in edges
        assert not ({("A", "B"), ("B", "A")} <= edges)  # one of the two dropped

        # The ORM tree is finite.
        def depth(g, d=0):
            assert d < 10
            return max([depth(c, d + 1) for c in g.children] or [d])
        depth(db_session.get(Genre, "X"))
        depth(db_session.get(Genre, "Y"))


class TestRootGenreTag:
    def test_crawled_known_book_gets_root_genre(self, db_session):
        from app.sync.ingest import tag_root_genre

        db_session.add(Genre(id="201583", name="r", slug="r", url="/r/"))
        db_session.add(_book(1))
        db_session.flush()
        tag_root_genre(db_session, 1, "201583")
        tag_root_genre(db_session, 1, "201583")  # idempotent
        db_session.flush()
        assert db_session.query(BookGenre).filter_by(book_id=1).count() == 1


# ---------------------------------------------------------------------------
# run_bulk: S1 end-to-end, S7 retry, S9 empty page, S16 stamping, heartbeat
# ---------------------------------------------------------------------------


def _page(books, *, total, next_offset=None):
    return MagicMock(books=books, total_count=total, next_offset=next_offset)


class TestRunBulk:
    def _run(self, factory, tmp_path, pages, **kw):
        from app.sync import bulk

        with patch("app.sync.bulk.SessionLocal", factory), \
             patch("app.db.SessionLocal", factory), \
             patch("app.sync.bulk.RateLimitedClient"), \
             patch("app.sync.bulk.time.sleep"), \
             patch("app.config.config.Config.SYNC_DIR", str(tmp_path)), \
             patch("app.sync.bulk.fetch_catalog_page", side_effect=pages):
            bulk.run_bulk(**kw)
        with factory() as s:
            return s.query(SyncRun).order_by(SyncRun.id.desc()).first()

    def test_series_403_keeps_new_book(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        with patch("app.sync.ingest.fetch_arts_detail", return_value=_detail(1, [_sref(70, 1)])), \
             patch("app.sync.ingest.fetch_series_page", side_effect=_http_error(403)):
            run = self._run(factory, tmp_path, [_page([_art(1)], total=1)], max_pages=1)
        assert run.status == "done" and run.books_failed == 0
        with factory() as s:
            assert s.get(Book, 1) is not None
            assert s.get(BookSeries, {"book_id": 1, "series_id": 70}) is not None
            assert s.get(BookGenre, {"book_id": 1, "genre_id": "201583"}) is not None
        assert not list(tmp_path.glob("failed_books_*"))

    def test_transport_error_and_429_are_retried(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        pages = [httpx.ReadTimeout("slow"), _http_error(429, {"retry-after": "5"}), _page([], total=0)]
        run = self._run(factory, tmp_path, pages)
        # third answer is an empty page at offset 0 -> S9 interrupt, but no crash
        assert run.status == "interrupted"
        assert "Empty catalog page" in run.error_message

    def test_full_run_stamps_and_delta_does_not(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        with factory() as s:
            s.add(_book(1))
            s.commit()
        run = self._run(factory, tmp_path, [_page([_art(1)], total=1)], max_pages=1)
        assert run.status == "done" and run.heartbeat_at is not None
        with factory() as s:
            assert s.query(SyncConfig).first().last_bulk_sync_at is None
        run = self._run(factory, tmp_path, [_page([_art(1)], total=1)])
        with factory() as s:
            assert s.query(SyncConfig).first().last_bulk_sync_at is not None

    def test_end_of_catalog_past_total_is_done(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        with factory() as s:
            s.add(_book(1))
            s.commit()
        pages = [_page([_art(1)], total=24, next_offset=24), _page([], total=24)]
        run = self._run(factory, tmp_path, pages)
        assert run.status == "done"

    def test_maintenance_runs_only_when_enabled(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        with factory() as s:
            s.add(_book(1))
            s.commit()
        with patch("app.sync.refresh.refresh_series_rosters") as rr, \
             patch("app.sync.refresh.refresh_stale_books") as rb:
            self._run(factory, tmp_path, [_page([_art(1)], total=1)], max_pages=1)
            assert not rr.called and not rb.called
            self._run(factory, tmp_path, [_page([_art(1)], total=1)], max_pages=1,
                      refresh_series=5, refresh_books=7)
        assert rr.call_args.kwargs["limit"] == 5
        assert rb.call_args.kwargs["limit"] == 7


class TestMemberBudget:
    def test_budget_stops_roster_and_leaves_it_unsynced(self, db_session):
        from app.sync.ingest import MemberBudget, ingest_series

        with patch("app.sync.ingest.fetch_series_page", return_value=_roster(6, [1, 2, 3, 4])), \
             patch("app.sync.ingest.fetch_arts_detail",
                   side_effect=lambda c, i: _detail(i, [_sref(6, i)])) as fad:
            budget = MemberBudget(2)
            assert ingest_series(db_session, MagicMock(), 6, "S", "/series/s-6/", 4, budget=budget) == 2
            assert fad.call_count == 2 and budget.remaining == 0
            assert db_session.get(Series, 6).roster_synced_at is None
            # next refresh continues with the unlinked members only
            assert ingest_series(db_session, MagicMock(), 6, "S", "/series/s-6/", 4,
                                 budget=MemberBudget(10)) == 2
        assert [c.args[1] for c in fad.call_args_list] == [1, 2, 3, 4]
        assert db_session.get(Series, 6).roster_synced_at is not None

    def test_roster_refresh_stops_when_budget_spent(self, db_session):
        from app.sync.refresh import refresh_series_rosters

        db_session.add_all([Series(id=i, name="s", url=f"/series/a-{i}/", book_count=1) for i in (1, 2, 3)])
        db_session.commit()

        def spend(session, client, sid, name, url, count, budget, **kw):
            budget.remaining -= 5
            return 0

        with patch("app.sync.refresh.ingest_series", side_effect=spend) as ing:
            stats = refresh_series_rosters(db_session, MagicMock(), limit=3, member_calls=8)
        assert ing.call_count == 2 and stats["series"] == 2


class TestWriteLockWindows:
    def test_roster_fetched_before_any_write(self, db_session):
        from app.sync.ingest import ingest_series

        def fetch(*a, **kw):
            assert not db_session.new and not db_session.dirty
            return _roster(12, [])

        with patch("app.sync.ingest.fetch_series_page", side_effect=fetch) as f:
            ingest_series(db_session, MagicMock(), 12, "S", "/series/s-12/", 0, trigger=None)
        assert f.called and db_session.get(Series, 12) is not None

    def test_bulk_commits_each_book(self, tmp_path):
        """The crawl commits after every book, not once per page, so the SQLite
        write lock never spans a page of network calls (W1). (With the current
        pysqlite settings a released outermost SAVEPOINT already commits; the
        explicit commit keeps this true if the engine gets the SQLAlchemy
        pysqlite transaction fix.)"""
        from sqlalchemy.orm import Session as _S

        from app.sync import bulk

        factory = _factory()
        _seed_config(factory)
        with factory() as s:
            s.add_all([_book(i) for i in range(1, 6)])
            s.commit()

        real_commit = _S.commit
        calls = []

        def counting_commit(self_):
            calls.append(1)
            return real_commit(self_)

        books = [_art(i) for i in range(1, 6)]
        with patch.object(_S, "commit", counting_commit), \
             patch("app.sync.bulk.SessionLocal", factory), patch("app.db.SessionLocal", factory), \
             patch("app.sync.bulk.RateLimitedClient"), \
             patch("app.config.config.Config.SYNC_DIR", str(tmp_path)), \
             patch("app.sync.bulk.fetch_catalog_page", return_value=_page(books, total=5)):
            bulk.run_bulk(max_pages=1)
        # open run + heartbeat + 5 books + page commit + finalise (>= 9)
        assert len(calls) >= 2 + len(books) + 1
        with factory() as s:
            assert s.query(SyncRun).first().status == "done"
            assert s.query(BookGenre).filter_by(genre_id="201583").count() == 5


class TestAdoptRun:
    """W13: run_bulk(run_id=...) takes over a row the admin request inserted."""

    def _insert(self, factory, **kw):
        with factory() as s:
            r = SyncRun(sync_config_id=1, type=kw.get("type", "bulk"), status=kw.get("status", "running"),
                        started_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC))
            s.add(r)
            s.commit()
            return r.id

    def _run(self, factory, tmp_path, **kw):
        from app.sync import bulk

        with patch("app.sync.bulk.SessionLocal", factory), patch("app.db.SessionLocal", factory), \
             patch("app.sync.bulk.RateLimitedClient"), \
             patch("app.config.config.Config.SYNC_DIR", str(tmp_path)), \
             patch("app.sync.bulk.fetch_catalog_page", return_value=_page([_art(1)], total=1)):
            bulk.run_bulk(max_pages=1, **kw)

    def test_adopts_preinserted_row(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        with factory() as s:
            s.add(_book(1))
            s.commit()
        rid = self._insert(factory)
        self._run(factory, tmp_path, run_id=rid)
        with factory() as s:
            runs = s.query(SyncRun).all()
            assert len(runs) == 1 and runs[0].id == rid and runs[0].status == "done"

    def test_refuses_when_another_catalog_run_is_live(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        other = self._insert(factory)
        mine = self._insert(factory)
        with pytest.raises(RuntimeError, match=f"id={other}"):
            self._run(factory, tmp_path, run_id=mine)
        with factory() as s:
            assert s.get(SyncRun, mine).status == "failed"
            assert s.get(SyncRun, other).status == "running"

    def test_refuses_non_running_row(self, tmp_path):
        factory = _factory()
        _seed_config(factory)
        rid = self._insert(factory, status="done")
        with pytest.raises(RuntimeError, match="Cannot adopt"):
            self._run(factory, tmp_path, run_id=rid)
