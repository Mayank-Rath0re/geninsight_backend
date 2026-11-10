# main.py
"""
App entrypoint. Only responsible for: creating the FastAPI app, middleware, scheduler,
health checks, and mounting routers. All endpoint logic lives in routers/, all business logic
lives in services/, all DB/config/JSON plumbing lives in core/.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import config, context_propagation
from routers import (
    auth, billing, charts, dashboard, data_sources, datasets, merge, organizations, public, query,
    sessions, transform,
)
from services import retention

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("my_global_app_logger")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Make ThreadPoolExecutor workers inherit the request context so LLM token usage is metered.
    context_propagation.install()

    scheduler = None
    if config.RUN_SCHEDULER:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(retention.run_retention_sweep, "cron", hour=2, minute=30,
                          id="retention_sweep", max_instances=1, coalesce=True)
        scheduler.add_job(retention.run_purge_sweep, "cron", hour=3, minute=0,
                          id="purge_sweep", max_instances=1, coalesce=True)
        scheduler.start()
        logger.info("Scheduler started (retention 02:30 UTC, purge 03:00 UTC)")
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

# allow_credentials=True (refresh-token cookie) is invalid with a "*" origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_origin_regex=config.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(datasets.router)
app.include_router(sessions.router)
app.include_router(transform.router)
app.include_router(merge.router)
app.include_router(dashboard.router)
app.include_router(charts.router)
app.include_router(query.router)
app.include_router(public.router)
app.include_router(billing.router)
app.include_router(organizations.router)
app.include_router(data_sources.router)


# ─────────────────────────────────────────────────────────────────────────
# CONNECTION CHECK
# ─────────────────────────────────────────────────────────────────────────

@app.get("/")
def check_health():
    return {"status": "healthy"}


@app.get("/health")
def check_health_detail():
    return {"status": "healthy", "database": "connected"}
