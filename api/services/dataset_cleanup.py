# dataset_cleanup.py

import logging
import re

from core import db

logger = logging.getLogger("my_global_app_logger")

UNSAFE_TABLE_NAME = re.compile(r"[\[\]\x00-\x1f;]")


def soft_delete_dataset(cur, table_id: int) -> None:
    cur.execute(
        "UPDATE dashboards SET is_deleted = 1, deleted_at = SYSDATETIME(), is_shared = 0, "
        "share_token = NULL, shared_snapshot = NULL, shared_at = NULL "
        "WHERE table_id = %s AND is_deleted = 0",
        (table_id,),
    )
    cur.execute(
        "UPDATE sessions SET is_deleted = 1, deleted_at = SYSDATETIME() "
        "WHERE table_id = %s AND is_deleted = 0",
        (table_id,),
    )
    cur.execute(
        "UPDATE table_info SET is_deleted = 1, deleted_at = SYSDATETIME() WHERE id = %s",
        (table_id,),
    )


def is_merge_source(table_id: int) -> bool:
    count = db.get_single_value(
        "SELECT COUNT(*) FROM queries q "
        "JOIN sessions src ON src.session_id = q.source_session_id "
        "JOIN sessions tgt ON tgt.session_id = q.session_id "
        "WHERE src.table_id = %s AND q.is_deleted = 0 AND tgt.is_deleted = 0 AND tgt.table_id <> %s",
        (table_id, table_id),
    )
    return int(count or 0) > 0


def drop_physical_table_if_unused(table_id: int, table_name: str) -> bool:
    if not table_name or UNSAFE_TABLE_NAME.search(table_name):
        logger.warning("Refusing to drop table with unsafe name for table_id %s", table_id)
        return False
    if is_merge_source(table_id):
        logger.info("Keeping physical table for %s: still used as a merge source", table_id)
        return False

    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"IF OBJECT_ID(N'[{table_name}]', N'U') IS NOT NULL DROP TABLE [{table_name}]"
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("Failed to drop physical table for table_id %s", table_id)
        return False
    finally:
        conn.close()