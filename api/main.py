# main.py
"""
App entrypoint. Only responsible for: creating the FastAPI app, middleware,
health checks, and mounting routers. All endpoint logic lives in routers/,
all business logic lives in services/, all DB/config/JSON plumbing lives
in core/.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, dashboard, datasets, merge, sessions, transform

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("my_global_app_logger")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # your actual frontend origin — exact scheme+host+port
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


# ─────────────────────────────────────────────────────────────────────────
# CONNECTION CHECK
# ─────────────────────────────────────────────────────────────────────────

@app.get("/")
def check_health():
    return {"status": "healthy"}


@app.get("/health")
def check_health_detail():
    return {"status": "healthy", "database": "connected"}