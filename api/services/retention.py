# services/retention.py
"""
Data retention jobs (run by the scheduler in main.py).

  run_retention_sweep()  hide datasets older than the owner's plan retention
                         (soft-delete: dataset, its sessions and dashboards disappear)
  run_purge_sweep()      physically drop the tables of datasets that have been hidden for
                         PURGE_GRACE_DAYS, then stamp table_info.purged_at

Retention follows the user's CURRENT plan. To avoid wiping data the moment a paid plan lapses,
a Free-plan user whose last paid period ended within DOWNGRADE_GRACE_DAYS is skipped.
Datasets backed by an external data source (data_source_id IS NOT NULL) are never touched.
"""

import logging
from datetime import timedelta

from core import config, db
from services import billing_service, dataset_cleanup

logger = logging.getLogger("my_global_app_logger")


def run_retention_sweep() -> dict:
    now = billing_service.utcnow()
    min_days = db.get_single_value("SELECT MIN(retention_days) FROM plans WHERE retention_days IS NOT NULL")
    if not min_days:
        return {"users": 0, "hidden": 0}

    cutoff_floor = now - timedelta(days=int(min_days))
    users = db.fetch(
        "SELECT DISTINCT user_id FROM table_info "
        "WHERE is_deleted = 0 AND data_source_id IS NULL AND user_id IS NOT NULL AND createdAt < %s",
        (cutoff_floor,),
    )
    hidden = 0
    for (user_id,) in users:
        try:
            ent = billing_service.get_entitlement(user_id)
            days = ent.plan.retention_days
            if days is None:
                continue  # retained forever

            if ent.plan.code == "free":
                last_end = db.get_single_value(
                    "SELECT MAX(current_period_end) FROM subscriptions WHERE owner_user_id = %s",
                    (user_id,),
                )
                if last_end and last_end > now - timedelta(days=config.DOWNGRADE_GRACE_DAYS):
                    continue

            expired = db.fetch(
                "SELECT id FROM table_info "
                "WHERE user_id = %s AND is_deleted = 0 AND data_source_id IS NULL AND createdAt < %s",
                (user_id, now - timedelta(days=days)),
            )
            for (table_id,) in expired:
                with db.transaction() as cur:
                    dataset_cleanup.soft_delete_dataset(cur, table_id)
                hidden += 1
        except Exception:
            logger.exception("Retention sweep failed for user %s", user_id)
    logger.info("Retention sweep: %s users checked, %s datasets hidden", len(users), hidden)
    return {"users": len(users), "hidden": hidden}


def run_purge_sweep() -> dict:
    grace = int(config.PURGE_GRACE_DAYS)  # int() keeps the interpolation below injection-safe
    rows = db.fetch(
        "SELECT id, name FROM table_info "
        "WHERE is_deleted = 1 AND purged_at IS NULL AND data_source_id IS NULL "
        f"AND deleted_at < DATEADD(day, -{grace}, SYSDATETIME())"
    )
    purged = 0
    for table_id, name in rows:
        try:
            if dataset_cleanup.drop_physical_table_if_unused(table_id, name):
                db.run("UPDATE table_info SET purged_at = SYSDATETIME() WHERE id = %s", (table_id,))
                purged += 1
        except Exception:
            logger.exception("Purge failed for table %s", table_id)
    logger.info("Purge sweep: %s of %s candidate tables dropped", purged, len(rows))
    return {"candidates": len(rows), "purged": purged}
