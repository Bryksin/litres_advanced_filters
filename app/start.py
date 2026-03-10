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

    from app.middleware import register_session_middleware
    register_session_middleware(app)

    from app.controllers.catalog import bp as catalog_bp
    app.register_blueprint(catalog_bp)

    from app.controllers.admin import bp as admin_bp
    app.register_blueprint(admin_bp)

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
