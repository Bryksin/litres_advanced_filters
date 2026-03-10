"""Display card dataclasses for catalog view models."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Card:
    type: str           # "book" | "series"
    title: str
    cover_url: str | None
    url: str
    rating_avg: float | None
    rating_count: int
    release_date: datetime | None = None


@dataclass
class BookCard(Card):
    book_id: int = 0
    authors: list[str] = field(default_factory=list)
    narrator: str | None = None


@dataclass
class SeriesCard(Card):
    series_id: int = 0
    book_count: int = 0
    authors: list[str] = field(default_factory=list)
