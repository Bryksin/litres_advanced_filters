"""Flask application factory."""

import logging

from flask import Flask, redirect, send_from_directory

from app.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=Config.TEMPLATE_FOLDER,
        static_folder=Config.STATIC_FOLDER,
    )
    app.config.from_object(Config)

    if app.config["SECRET_KEY"] == "dev-secret-change-in-production":
        logging.getLogger(__name__).warning(
            "SECRET_KEY is using the default dev value — set SECRET_KEY env var for production"
        )

    _reap_zombie_sync_runs_on_boot()

    from app.middleware import register_session_middleware
    register_session_middleware(app)

    from app.controllers.catalog import bp as catalog_bp
    app.register_blueprint(catalog_bp)

    from app.controllers.admin_panel import bp as admin_panel_bp
    app.register_blueprint(admin_panel_bp)

    from app.controllers.auth import bp as auth_bp
    app.register_blueprint(auth_bp)

    @app.route("/covers/<int:art_id>")
    def cover(art_id: int):
        import os
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = os.path.join(Config.COVERS_DIR, f"{art_id}.{ext}")
            if os.path.exists(path):
                return send_from_directory(Config.COVERS_DIR, f"{art_id}.{ext}")
        return redirect("/static/img/cover-placeholder.svg")

    return app


def _reap_zombie_sync_runs_on_boot() -> None:
    """Mark any sync_run rows left in 'running' from a previous container run.

    Crashes that bypass the bulk.py finally block (SIGKILL, OOM, host crash)
    leave zombie rows that block future syncs and confuse the admin UI. This
    self-heal runs once per process boot and is best-effort: any failure is
    logged but does not prevent the app from starting.
    """
    log = logging.getLogger(__name__)
    try:
        from app.db import SessionLocal
        from app.sync.common import reap_zombie_sync_runs

        with SessionLocal() as session:
            reaped = reap_zombie_sync_runs(session)
            if reaped:
                log.info("Boot: reaped %d zombie sync_run row(s)", reaped)
    except Exception:
        log.exception("Boot: zombie sync_run reaper failed (non-fatal)")
