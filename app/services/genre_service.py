"""Genre service — genre tree queries for the sidebar."""

from sqlalchemy.orm import Session, selectinload

from app.db.models import Genre

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


def get_genre_ancestor_ids(session: Session, genre_id: str) -> set[str]:
    """Return genre_id plus all ancestor IDs up to the root.

    Used by the template to decide which <details> nodes to open.
    """
    ids: set[str] = set()
    current_id: str | None = genre_id
    while current_id is not None:
        ids.add(current_id)
        genre = session.get(Genre, current_id)
        if genre is None:
            break
        current_id = genre.parent_id
    return ids


def get_genre_descendant_ids(session: Session, genre_id: str) -> set[str]:
    """Return genre_id plus all descendant IDs (children, grandchildren, etc.).

    Used for parent genre selection — clicking a parent genre shows books
    from all its sub-genres.
    """
    ids: set[str] = {genre_id}
    queue = [genre_id]
    while queue:
        parent_id = queue.pop()
        children = (
            session.query(Genre.id)
            .filter(Genre.parent_id == parent_id)
            .all()
        )
        for (child_id,) in children:
            if child_id not in ids:
                ids.add(child_id)
                queue.append(child_id)
    return ids
