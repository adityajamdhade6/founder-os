"""Vercel entrypoint: exposes the FastAPI app as a serverless function."""
from founderos.api import app  # noqa: F401
