"""Unit tests for LitRes auth scraper — mocked HTTP, no network."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import User


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.side_effect = (
        None if status_code < 400 else Exception(f"HTTP {status_code}")
    )
    return resp


LOGIN_SUCCESS_RESPONSE = {
    "payload": {
        "data": {
            "access_token": "fake-jwt-token",
            "refresh_token": "fake-refresh-hex",
            "token_type": "session",
            "expires_in": 900,
            "sid": "fake-sid",
        }
    }
}


class TestLitresLogin:
    def test_login_success_returns_auth_result(self):
        from app.scrapers.auth import litres_login, AuthResult

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_response(200, LOGIN_SUCCESS_RESPONSE)

        result = litres_login(mock_client, "user@example.com", "password123")

        assert isinstance(result, AuthResult)
        assert result.access_token == "fake-jwt-token"
        assert result.refresh_token == "fake-refresh-hex"
        assert result.expires_in == 900
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/auth/login" in call_args[0][0]

    def test_login_wrong_credentials(self):
        from app.scrapers.auth import litres_login, LitresAuthError

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_response(401, {
            "error": "Unauthorized"
        })

        with pytest.raises(LitresAuthError, match="Authentication failed"):
            litres_login(mock_client, "user@example.com", "wrong-password")

    def test_login_unexpected_response(self):
        from app.scrapers.auth import litres_login, LitresAuthError

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_response(200, {
            "payload": {"data": {}}
        })

        with pytest.raises(LitresAuthError, match="No access_token"):
            litres_login(mock_client, "user@example.com", "password123")

    def test_login_network_error(self):
        from app.scrapers.auth import litres_login, LitresAuthError
        import httpx

        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(LitresAuthError, match="Connection"):
            litres_login(mock_client, "user@example.com", "password123")


class TestLitresRefresh:
    def test_refresh_success(self):
        from app.scrapers.auth import litres_refresh, AuthResult

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_response(200, {
            "payload": {
                "data": {
                    "access_token": "new-jwt",
                    "refresh_token": "new-refresh-hex",
                    "expires_in": 900,
                }
            }
        })

        result = litres_refresh(mock_client, "old-refresh-hex")

        assert isinstance(result, AuthResult)
        assert result.access_token == "new-jwt"
        assert result.refresh_token == "new-refresh-hex"

    def test_refresh_invalid_token_raises(self):
        from app.scrapers.auth import litres_refresh, LitresAuthError

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_response(401, {"error": "invalid_token"})

        with pytest.raises(LitresAuthError, match="refresh failed"):
            litres_refresh(mock_client, "expired-refresh")


@pytest.fixture
def auth_db():
    """In-memory DB with a User for token storage tests."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Factory() as s:
        s.add(User(id=1, created_at=datetime.now(timezone.utc)))
        s.commit()
    return Factory


class TestGetValidToken:
    @patch("app.scrapers.auth.litres_login")
    def test_fresh_login_when_no_stored_tokens(self, mock_login, auth_db):
        from app.scrapers.auth import get_valid_token, AuthResult

        mock_login.return_value = AuthResult(
            access_token="new-jwt", refresh_token="new-refresh", expires_in=900
        )

        token = get_valid_token(
            session_factory=auth_db,
            user_id=1,
            email="user@example.com",
            password="pass123",
        )

        assert token == "new-jwt"
        mock_login.assert_called_once()

        # Verify tokens persisted in User.session_data
        with auth_db() as s:
            user = s.get(User, 1)
            data = json.loads(user.session_data)
            assert data["access_token"] == "new-jwt"
            assert data["refresh_token"] == "new-refresh"
            assert data["email"] == "user@example.com"

    def test_reuses_valid_stored_token(self, auth_db):
        """If stored access_token hasn't expired, use it directly — no HTTP calls."""
        from app.scrapers.auth import get_valid_token

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with auth_db() as s:
            user = s.get(User, 1)
            user.session_data = json.dumps({
                "email": "user@example.com",
                "access_token": "still-valid-jwt",
                "refresh_token": "some-refresh",
                "expires_at": future,
            })
            s.commit()

        token = get_valid_token(
            session_factory=auth_db,
            user_id=1,
            email="user@example.com",
            password="pass123",
        )

        assert token == "still-valid-jwt"

    @patch("app.scrapers.auth.litres_refresh")
    def test_refreshes_expired_token(self, mock_refresh, auth_db):
        from app.scrapers.auth import get_valid_token, AuthResult

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with auth_db() as s:
            user = s.get(User, 1)
            user.session_data = json.dumps({
                "email": "user@example.com",
                "access_token": "expired-jwt",
                "refresh_token": "valid-refresh",
                "expires_at": past,
            })
            s.commit()

        mock_refresh.return_value = AuthResult(
            access_token="refreshed-jwt", refresh_token="new-refresh", expires_in=900
        )

        token = get_valid_token(
            session_factory=auth_db,
            user_id=1,
            email="user@example.com",
            password="pass123",
        )

        assert token == "refreshed-jwt"
        mock_refresh.assert_called_once()

    @patch("app.scrapers.auth.litres_login")
    @patch("app.scrapers.auth.litres_refresh")
    def test_relogin_when_refresh_fails(self, mock_refresh, mock_login, auth_db):
        from app.scrapers.auth import get_valid_token, AuthResult, LitresAuthError

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with auth_db() as s:
            user = s.get(User, 1)
            user.session_data = json.dumps({
                "email": "user@example.com",
                "access_token": "expired-jwt",
                "refresh_token": "dead-refresh",
                "expires_at": past,
            })
            s.commit()

        mock_refresh.side_effect = LitresAuthError("refresh failed")
        mock_login.return_value = AuthResult(
            access_token="fresh-jwt", refresh_token="fresh-refresh", expires_in=900
        )

        token = get_valid_token(
            session_factory=auth_db,
            user_id=1,
            email="user@example.com",
            password="pass123",
        )

        assert token == "fresh-jwt"
        mock_login.assert_called_once()
