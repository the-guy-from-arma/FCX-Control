from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from fcx_engine.service import app

from .api import router


app.title = "FCX / Ravenhood / FEC Control Platform"
app.version = "1.0.0"
app.include_router(router)

STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="fcx-control-pwa")

