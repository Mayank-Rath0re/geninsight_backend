# routers/dashboard.py
import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import db
from services import dashboard as dashboard_service

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["dashboard"])


class DashboardIntentBasicRequest(BaseModel):
    userId: int
    table_id: int
    prompt: str


@router.post("/dashboard_intent_basic")
def dashboard_intent_basic(payload: DashboardIntentBasicRequest):
    userId = payload.userId
    table_id = payload.table_id
    prompt = payload.prompt
    try:
        q = "SELECT metadata, knowledgebase FROM table_info WHERE id = %s"
        row = db.fetch(q, params=(table_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Table not found.")
        metadata = json.loads(row[0][0])
        knowledge_base = json.loads(row[0][1])

        statement = "SELECT name FROM table_info WHERE id = %s"
        table_name = db.get_single_value(statement, (table_id,))
        if not table_name:
            raise HTTPException(status_code=404, detail="Table name not found.")

        dashboard_spec_raw = dashboard_service.extract_dashboard_intent(
            table_id, prompt, knowledge_base, metadata, table_name
        )
        dashboard_spec = json.dumps(dashboard_spec_raw)

        # dashboards requires user_id and table_id (table_id is NOT NULL in schema)
        statement = (
            "INSERT INTO dashboards (user_id, table_id, dashboard_intent) "
            "OUTPUT INSERTED.dashboard_id VALUES (%s, %s, %s)"
        )
        dashboard_obj_id = db.insert(statement, (userId, table_id, dashboard_spec))

        show_dashboard_intent = dashboard_service.render_dashboard_summary(dashboard_spec_raw)
        return {
            "dashboard_id": dashboard_obj_id,
            "dashboard_intent": show_dashboard_intent,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Dashboard intent failed for table %s", table_id)
        raise HTTPException(status_code=500, detail=f"Dashboard intent failed: {str(e)}")


class CrossQuestioningRequest(BaseModel):
    dashboard_id: int
    update_prompt: str


@router.post("/cross_questioning")
def cross_questioning(payload: CrossQuestioningRequest):
    dashboard_id = payload.dashboard_id
    update_prompt = payload.update_prompt
    try:
        user_feedback = dashboard_service.collect_feedback(update_prompt)
        db.run(
            "UPDATE dashboards SET user_response = %s WHERE dashboard_id = %s",
            (user_feedback, dashboard_id),
        )

        q = "SELECT dashboard_intent, table_id FROM dashboards WHERE dashboard_id = %s"
        row = db.fetch(q, params=(dashboard_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Dashboard not found.")
        dashboard_intent = json.loads(row[0][0])
        table_id = row[0][1]

        q2 = "SELECT metadata FROM table_info WHERE id = %s"
        metadata_raw = db.get_single_value(q2, (table_id,))
        if not metadata_raw:
            raise HTTPException(status_code=404, detail="Table metadata not found.")
        metadata = json.loads(metadata_raw)

        if user_feedback not in ["skip", "confirm", "okay", "go"]:
            updated_dashboard_intent = dashboard_service.apply_dashboard_feedback(
                dashboard_intent, user_feedback, metadata
            )
        else:
            updated_dashboard_intent = dashboard_intent

        db.run(
            "UPDATE dashboards SET dashboard_intent = %s WHERE dashboard_id = %s",
            (json.dumps(updated_dashboard_intent), dashboard_id),
        )
        return {"dashboard_id": dashboard_id, "dashboard_intent": updated_dashboard_intent}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Cross questioning failed for dashboard %s", dashboard_id)
        raise HTTPException(status_code=500, detail=f"Cross questioning failed: {str(e)}")
