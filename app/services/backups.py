"""Native backend snapshot bridge used before controller updates."""
from db import get_db


def maybe_create_pre_action_backup(trigger: str) -> dict:
    from backend.backups import BackupService
    try:
        return BackupService(get_db()).create_snapshot(trigger)
    except Exception:
        return {'status': 'failed', 'message': 'Backend configuration backup failed'}
