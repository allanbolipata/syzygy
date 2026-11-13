"""Syzygy app package.

The FastAPI Sample Pairing Service lives here: ``syzygy.main`` builds the
app, ``syzygy/routers/`` holds the endpoint routers, and the cohort trio
( ``syzygy/cohort/`` ) covers SMILE cohort validate/CRF handling.
Vendored data files (cohort JSON schemas) live in ``syzygy/data/``.

Run: ``uvicorn syzygy.main:app`` from the repo root.
"""
