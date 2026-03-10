"""Tests for genre sync logging + retry (BUG-4 fix)."""

import logging
from unittest.mock import MagicMock, patch

import httpx
from httpx import Response, Request

from app.scrapers.genres import _fetch_subgenre_page


class TestFetchSubgenrePage:
    """Tests for _fetch_subgenre_page retry logic."""

    @patch("app.scrapers.genres.time.sleep")
    def test_retries_on_503_then_succeeds(self, mock_sleep):
        """A single 503 is retried and succeeds on second attempt."""
        request = Request("GET", "https://www.litres.ru/genre/test-123/")
        response = Response(503, request=request)
        error = httpx.HTTPStatusError("503", request=request, response=response)

        client = MagicMock()
        client.get_html.side_effect = [error, "<html>ok</html>"]

        result = _fetch_subgenre_page(client, "/genre/test-123/", "Test")
        assert result == "<html>ok</html>"
        assert client.get_html.call_count == 2
        mock_sleep.assert_called_once()

    @patch("app.scrapers.genres.time.sleep")
    def test_returns_none_after_all_retries_exhausted(self, mock_sleep):
        """After all retries exhausted, returns None."""
        request = Request("GET", "https://www.litres.ru/genre/test-123/")
        response = Response(503, request=request)
        error = httpx.HTTPStatusError("503", request=request, response=response)

        client = MagicMock()
        client.get_html.side_effect = error  # Always fails

        result = _fetch_subgenre_page(client, "/genre/test-123/", "Test")
        assert result is None
        # 1 initial + 2 retries = 3 total
        assert client.get_html.call_count == 3

    def test_no_retry_on_non_http_error(self):
        """Non-HTTP errors are not retried."""
        client = MagicMock()
        client.get_html.side_effect = ValueError("parse error")

        result = _fetch_subgenre_page(client, "/genre/test-123/", "Test")
        assert result is None
        assert client.get_html.call_count == 1

    def test_no_retry_on_4xx(self):
        """4xx errors are not retried."""
        request = Request("GET", "https://www.litres.ru/genre/test-123/")
        response = Response(404, request=request)
        error = httpx.HTTPStatusError("404", request=request, response=response)

        client = MagicMock()
        client.get_html.side_effect = error

        result = _fetch_subgenre_page(client, "/genre/test-123/", "Test")
        assert result is None
        assert client.get_html.call_count == 1


class TestRunGenresLogging:
    """Tests that run_genres() uses file logging."""

    @patch("app.sync.genres.SessionLocal")
    @patch("app.sync.genres.RateLimitedClient")
    @patch("app.sync.genres.fetch_genre_tree_hierarchical", return_value=[])
    @patch("app.sync.genres.setup_sync_logging")
    def test_calls_setup_sync_logging(self, mock_setup, mock_fetch, mock_client_cls, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

        from app.sync.genres import run_genres
        run_genres(verbose=False)

        mock_setup.assert_called_once()
        assert mock_setup.call_args[1]["verbose"] is False

    @patch("app.sync.genres.SessionLocal")
    @patch("app.sync.genres.RateLimitedClient")
    @patch("app.sync.genres.fetch_genre_tree_hierarchical", return_value=[])
    @patch("app.sync.genres.setup_sync_logging")
    def test_passes_verbose_flag(self, mock_setup, mock_fetch, mock_client_cls, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

        from app.sync.genres import run_genres
        run_genres(verbose=True)

        mock_setup.assert_called_once()
        assert mock_setup.call_args[1]["verbose"] is True
