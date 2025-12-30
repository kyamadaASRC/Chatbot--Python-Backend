"""
Package initializer.

We avoid instantiating a Flask app at import time so `import app.app` loads the
actual module (needed for tests) instead of being shadowed by a package-level
attribute named `app`.
"""

from .app import create_app

__all__ = ["create_app"]

# Legacy compatibility: `from app import app` will lazily return a Flask app.
def __getattr__(name):
    if name == "app" or name == "application":
        return create_app()
    raise AttributeError(name)
