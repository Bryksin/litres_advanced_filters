"""Unit tests for genre service functions."""

from app.db.models import Genre
from app.services.genre_service import (
    LEGKOE_CHTENIE_ID,
    get_genre_ancestor_ids,
    get_genre_descendant_ids,
    get_genre_tree,
)


def test_get_genre_tree_returns_legkoe_chtenie_subtree(db_session):
    """get_genre_tree returns only the Легкое чтение root and its children."""
    db_session.add(Genre(id=LEGKOE_CHTENIE_ID, name="Легкое чтение", slug="legkoe", url="/genre/legkoe/"))
    db_session.add(Genre(id="5004", parent_id=LEGKOE_CHTENIE_ID, name="Фантастика", slug="fantastika", url="/genre/fantastika/"))
    db_session.add(Genre(id="9999", name="Other Root", slug="other", url="/genre/other/"))
    db_session.flush()

    roots = get_genre_tree(db_session)
    assert len(roots) == 1
    assert roots[0].id == LEGKOE_CHTENIE_ID
    assert len(roots[0].children) == 1
    assert roots[0].children[0].id == "5004"


def test_get_genre_tree_returns_empty_when_no_legkoe_chtenie(db_session):
    """get_genre_tree returns empty list if Легкое чтение doesn't exist."""
    db_session.add(Genre(id="100", name="Root", slug="root", url="/genre/root/"))
    db_session.flush()

    roots = get_genre_tree(db_session)
    assert roots == []


def test_get_genre_ancestor_ids_walks_up(db_session):
    """get_genre_ancestor_ids returns the full chain from leaf to root."""
    db_session.add(Genre(id="1", name="L1", slug="l1", url="/1/"))
    db_session.add(Genre(id="2", parent_id="1", name="L2", slug="l2", url="/2/"))
    db_session.add(Genre(id="3", parent_id="2", name="L3", slug="l3", url="/3/"))
    db_session.flush()

    ids = get_genre_ancestor_ids(db_session, "3")
    assert ids == {"1", "2", "3"}


def test_get_genre_ancestor_ids_missing_genre(db_session):
    """get_genre_ancestor_ids handles missing genre gracefully."""
    ids = get_genre_ancestor_ids(db_session, "nonexistent")
    assert ids == {"nonexistent"}


def test_get_genre_descendant_ids(db_session):
    """get_genre_descendant_ids returns parent + all children recursively."""
    db_session.add(Genre(id="1", name="Root", slug="root", url="/1/"))
    db_session.add(Genre(id="2", parent_id="1", name="L2", slug="l2", url="/2/"))
    db_session.add(Genre(id="3", parent_id="2", name="L3", slug="l3", url="/3/"))
    db_session.add(Genre(id="4", parent_id="1", name="L2b", slug="l2b", url="/4/"))
    db_session.flush()

    ids = get_genre_descendant_ids(db_session, "1")
    assert ids == {"1", "2", "3", "4"}


def test_get_genre_descendant_ids_leaf(db_session):
    """get_genre_descendant_ids for a leaf genre returns just itself."""
    db_session.add(Genre(id="1", name="Root", slug="root", url="/1/"))
    db_session.add(Genre(id="2", parent_id="1", name="Leaf", slug="leaf", url="/2/"))
    db_session.flush()

    ids = get_genre_descendant_ids(db_session, "2")
    assert ids == {"2"}
