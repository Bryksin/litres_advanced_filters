"""SettingsService — load/save CatalogQuery from UserSettings table."""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import UserSettings
from app.models.catalog_query import CatalogQuery


class SettingsService:
    def __init__(self, session: Session, user_id: int) -> None:
        self.session = session
        self.user_id = user_id

    def get(self) -> CatalogQuery:
        row = self.session.get(UserSettings, self.user_id)
        if row is None:
            return CatalogQuery(
                exclude_narrators=True,
                excluded_narrators=["Литрес Авточтец"],
            )
        return CatalogQuery(
            genre_id=row.genre_id,
            series_only=bool(row.series_only),
            standalones_only=bool(row.standalones_only),
            series_min=row.series_min,
            series_max=row.series_max,
            full_series_subscription=bool(row.full_series_subscription),
            exclude_authors=bool(row.exclude_authors),
            excluded_authors=json.loads(row.excluded_authors_json or "[]"),
            exclude_narrators=bool(row.exclude_narrators),
            excluded_narrators=json.loads(row.excluded_narrators_json or "[]"),
            rating_min=row.rating_min,
            rating_max=row.rating_max,
            hide_listened=bool(row.hide_listened),
            incomplete_series_only=bool(row.incomplete_series_only),
        )

    def reset(self) -> CatalogQuery:
        """Reset all filter settings to defaults, return clean query."""
        row = self.session.get(UserSettings, self.user_id)
        if row is None:
            return CatalogQuery()
        row.series_only = False
        row.standalones_only = False
        row.series_min = None
        row.series_max = None
        row.full_series_subscription = False
        row.exclude_authors = False
        row.excluded_authors_json = "[]"
        row.exclude_narrators = False
        row.excluded_narrators_json = '["Литрес Авточтец"]'
        row.rating_min = None
        row.rating_max = None
        row.hide_listened = False
        row.incomplete_series_only = False
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return CatalogQuery(
            genre_id=row.genre_id,
            excluded_narrators=["Литрес Авточтец"],
        )

    def save(self, query: CatalogQuery) -> None:
        row = self.session.get(UserSettings, self.user_id)
        if row is None:
            return
        row.genre_id = query.genre_id
        row.series_only = query.series_only
        row.standalones_only = query.standalones_only
        row.series_min = query.series_min
        row.series_max = query.series_max
        row.full_series_subscription = query.full_series_subscription
        row.exclude_authors = query.exclude_authors
        row.excluded_authors_json = json.dumps(query.excluded_authors)
        row.exclude_narrators = query.exclude_narrators
        row.excluded_narrators_json = json.dumps(query.excluded_narrators)
        row.rating_min = query.rating_min
        row.rating_max = query.rating_max
        row.hide_listened = query.hide_listened
        row.incomplete_series_only = query.incomplete_series_only
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()
