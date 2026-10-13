"""Vercel entrypoint: every route is rewritten here (see vercel.json)."""

from app.main import app

__all__ = ["app"]
