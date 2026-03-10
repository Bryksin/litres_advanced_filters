"""LitRes Advanced Filters application package.

Flask discovers create_app automatically when running `flask --app app run`.
No module-level side effects — importing this package is always safe.
"""

from app.start import create_app

__all__ = ["create_app"]
