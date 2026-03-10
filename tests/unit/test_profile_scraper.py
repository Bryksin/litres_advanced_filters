# tests/unit/test_profile_scraper.py
"""Unit tests for LitRes profile scraper — fetch finished books."""

from unittest.mock import MagicMock

import pytest


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.side_effect = (
        None if status_code < 400 else Exception(f"HTTP {status_code}")
    )
    return resp


class TestFetchFinishedBooks:
    def test_returns_book_ids(self):
        from app.scrapers.profile import fetch_finished_book_ids

        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(200, {
            "payload": {
                "data": [
                    {"id": 100, "title": "Book A", "is_finished": True},
                    {"id": 200, "title": "Book B", "is_finished": True},
                    {"id": 300, "title": "Book C", "is_finished": True},
                ]
            }
        })

        ids = fetch_finished_book_ids(mock_client, "fake-token")

        assert ids == [100, 200, 300]
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "/users/me/arts/finished" in call_args[0][0]
        assert "Bearer fake-token" in str(call_args)

    def test_empty_library(self):
        from app.scrapers.profile import fetch_finished_book_ids

        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(200, {
            "payload": {"data": []}
        })

        ids = fetch_finished_book_ids(mock_client, "fake-token")

        assert ids == []

    def test_auth_expired(self):
        from app.scrapers.profile import fetch_finished_book_ids, LitresProfileError

        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(401, {"error": "Unauthorized"})

        with pytest.raises(LitresProfileError, match="401"):
            fetch_finished_book_ids(mock_client, "expired-token")

    def test_filters_only_finished(self):
        """Endpoint should only return finished books, but verify we extract IDs correctly."""
        from app.scrapers.profile import fetch_finished_book_ids

        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(200, {
            "payload": {
                "data": [
                    {"id": 100, "is_finished": True},
                    {"id": 200, "is_finished": True},
                ]
            }
        })

        ids = fetch_finished_book_ids(mock_client, "token")
        assert len(ids) == 2

    def test_cursor_pagination_fetches_all_pages(self):
        """Verify cursor-based pagination follows next_page until exhausted."""
        from app.scrapers.profile import fetch_finished_book_ids

        page1_resp = _mock_response(200, {
            "payload": {
                "data": [{"id": i} for i in range(1, 101)],
                "pagination": {
                    "next_page": "/api/users/me/arts/finished?after=cursor_page2",
                    "previous_page": None,
                },
            }
        })
        page2_resp = _mock_response(200, {
            "payload": {
                "data": [{"id": i} for i in range(101, 201)],
                "pagination": {
                    "next_page": "/api/users/me/arts/finished?after=cursor_page3",
                    "previous_page": "/api/users/me/arts/finished?after=cursor_page1",
                },
            }
        })
        page3_resp = _mock_response(200, {
            "payload": {
                "data": [{"id": i} for i in range(201, 251)],
                "pagination": {
                    "next_page": None,
                    "previous_page": "/api/users/me/arts/finished?after=cursor_page2",
                },
            }
        })

        mock_client = MagicMock()
        mock_client.get.side_effect = [page1_resp, page2_resp, page3_resp]

        ids = fetch_finished_book_ids(mock_client, "token")

        assert len(ids) == 250
        assert ids[:3] == [1, 2, 3]
        assert ids[-1] == 250
        assert mock_client.get.call_count == 3

        # Verify cursor was passed on 2nd and 3rd calls
        second_call_params = mock_client.get.call_args_list[1][1].get("params") or mock_client.get.call_args_list[1][0][1] if len(mock_client.get.call_args_list[1][0]) > 1 else mock_client.get.call_args_list[1][1]["params"]
        assert second_call_params.get("after") == "cursor_page2"
