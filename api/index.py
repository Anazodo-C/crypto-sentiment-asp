"""Vercel entrypoint.

Vercel's Python runtime (@vercel/python) detects an ASGI `app` object in
this file and serves it directly - no separate adapter package needed for
FastAPI. This just re-exports the real app from app/main.py so there's
only one source of truth for routes/logic.

Note: Vercel serverless functions are stateless and cold-start per
request (no long-lived process), which is fine for this ASP since every
/sentiment call is already a fresh set of outbound HTTP calls anyway.
"""
import sys
from pathlib import Path

# Make the project root importable when Vercel runs this file standalone.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402
