# routers/data_sources.py
"""
Client-owned database connections (custom DBs).

  GET    /data_sources          list the caller's connections (no passwords)
  POST   /data_source           add a connection + register its tables
  POST   /update_data_source    edit connection details and/or exposed tables
  POST   /delete_data_source    revoke a connection (remote data is never touched)
  GET    /data_source_tables    list remote tables and which are registered
  POST   /data_source_tables_preview  list remote tables for UNSAVED credentials
  POST   /test_data_source      verify unsaved credentials
  POST   /run_data_source       run a read-only SELECT in the target's native dialect

Only SQL Server, MySQL and Postgres are supported. Passwords are encrypted at rest
and never returned. The public field names (dburl/dbport/dbname/dbuser/dbtype) map
to the data_sources columns documented in sql/init-scripts/init.sql.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.security import get_current_user_id, require_same_user
from services.custom_db import service as source_service

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["data_sources"])


def _as_dict(model) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


class TestDataSourceRequest(BaseModel):
    userId: int
    dburl: str
    dbport: Optional[int] = None
    dbname: str
    dbuser: str
    dbpassword: str
    dbtype: str
    ssl: bool = False


class CreateDataSourceRequest(BaseModel):
    userId: int
    name: Optional[str] = None
    dburl: str
    dbport: Optional[int] = None
    dbname: str
    dbuser: str
    dbpassword: str
    dbtype: str
    ssl: bool = False
    tables: Optional[List[str]] = None  # omit = expose every base table


class UpdateDataSourceRequest(BaseModel):
    userId: int
    data_source_id: int
    name: Optional[str] = None
    dburl: Optional[str] = None
    dbport: Optional[int] = None
    dbname: Optional[str] = None
    dbuser: Optional[str] = None
    dbpassword: Optional[str] = None
    dbtype: Optional[str] = None
    ssl: Optional[bool] = None
    tables: Optional[List[str]] = None  # omit = leave registered tables unchanged


class DataSourceIdRequest(BaseModel):
    userId: int
    data_source_id: int


class RunDataSourceRequest(BaseModel):
    userId: int
    data_source_id: int
    sql: str
    limit: Optional[int] = None


@router.get("/data_sources")
def list_data_sources(userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    try:
        return source_service.list_sources(userId)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list data sources for user %s", userId)
        raise HTTPException(status_code=500, detail="Failed to fetch database connections.")


@router.post("/data_source")
def create_data_source(payload: CreateDataSourceRequest,
                       authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    raw = _as_dict(payload)
    selected = raw.get("tables") if "tables" in raw else None
    try:
        return source_service.create_source(payload.userId, raw, selected_tables=selected)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add data source for user %s", payload.userId)
        raise HTTPException(status_code=500, detail=f"Failed to add database: {str(e)}")


@router.post("/update_data_source")
def update_data_source(payload: UpdateDataSourceRequest,
                       authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    raw = _as_dict(payload)
    selected = raw.get("tables") if "tables" in raw else None
    try:
        return source_service.update_source(
            payload.userId, payload.data_source_id, raw, selected_tables=selected
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update data source %s", payload.data_source_id)
        raise HTTPException(status_code=500, detail=f"Failed to update database: {str(e)}")


@router.post("/delete_data_source")
def delete_data_source(payload: DataSourceIdRequest,
                       authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    try:
        return source_service.delete_source(payload.userId, payload.data_source_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete data source %s", payload.data_source_id)
        raise HTTPException(status_code=500, detail=f"Failed to remove database: {str(e)}")


@router.get("/data_source_tables")
def data_source_tables(data_source_id: int, userId: int,
                       authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    try:
        return source_service.remote_tables(userId, data_source_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list remote tables for data source %s", data_source_id)
        raise HTTPException(status_code=500, detail="Failed to list tables from this database.")


@router.post("/test_data_source")
def test_data_source(payload: TestDataSourceRequest,
                     authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    return source_service.test_connection(_as_dict(payload))


@router.post("/data_source_tables_preview")
def data_source_tables_preview(payload: TestDataSourceRequest,
                               authed_user_id: int = Depends(get_current_user_id)):
    """Tables of an unsaved connection, so the Add-DB wizard can offer a selection."""
    require_same_user(authed_user_id, payload.userId)
    return source_service.preview_tables(_as_dict(payload))


@router.post("/run_data_source")
def run_data_source(payload: RunDataSourceRequest,
                    authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    return source_service.run_query(
        payload.userId, payload.data_source_id, payload.sql, limit=payload.limit
    )
