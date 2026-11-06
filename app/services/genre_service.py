"""Genre service — genre tree queries for the sidebar."""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Genre, GenreParent

# Root genre for the synced catalog scope
LEGKOE_CHTENIE_ID = "201583"


def get_genre_tree(session: Session) -> list[Genre]:
    """Return genre tree rooted at Легкое чтение with children eagerly loaded (3 levels).

    Only shows genres within the synced scope to avoid empty results.
    """
    root = (
        session.query(Genre)
        .filter(Genre.id == LEGKOE_CHTENIE_ID)
        .options(
            selectinload(Genre.children)
            .selectinload(Genre.children)
            .selectinload(Genre.children)
        )
        .first()
    )
    if root is None:
        return []
    return [root]


def _edges(session: Session) -> list[tuple[str, str]]:
    """All (genre_id, parent_id) edges: primary ``parent_id`` plus ``genre_parent``.

    The genre table is small (~500 rows), so one query beats a query per node.
    """
    rows = session.execute(
        select(Genre.id, Genre.parent_id).where(Genre.parent_id.isnot(None))
        .union(select(GenreParent.genre_id, GenreParent.parent_id))
    ).all()
    return [(g, p) for g, p in rows]


def get_genre_ancestor_ids(session: Session, genre_id: str) -> set[str]:
    """Return genre_id plus all ancestor IDs up to the root(s).

    A genre can have several parents (DAG), so every ancestor path is
    included. Used by the template to decide which <details> nodes to open.
    """
    parents: dict[str, set[str]] = {}
    for g, p in _edges(session):
        parents.setdefault(g, set()).add(p)
    ids: set[str] = {genre_id}
    queue = [genre_id]
    while queue:
        for parent_id in parents.get(queue.pop(), ()):
            if parent_id not in ids:
                ids.add(parent_id)
                queue.append(parent_id)
    return ids


def get_genre_descendant_ids(session: Session, genre_id: str) -> set[str]:
    """Return genre_id plus all descendant IDs (children, grandchildren, etc.).

    Used for parent genre selection — clicking a parent genre shows books
    from all its sub-genres. Follows every parent edge, so a genre listed
    under several parents counts as a descendant of each of them.
    """
    children: dict[str, set[str]] = {}
    for g, p in _edges(session):
        children.setdefault(p, set()).add(g)
    ids: set[str] = {genre_id}
    queue = [genre_id]
    while queue:
        for child_id in children.get(queue.pop(), ()):
            if child_id not in ids:
                ids.add(child_id)
                queue.append(child_id)
    return ids
