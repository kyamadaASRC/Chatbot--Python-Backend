from .app import create_app

# Expose module-level Flask app instances for CLI compatibility.
app = create_app()
application = app  # alias for some WSGI/CLI loaders

__all__ = ["create_app", "app", "application"]
