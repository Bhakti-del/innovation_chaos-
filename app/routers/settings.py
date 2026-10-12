"""
Settings router for SmartCanteen AI.

GET   /api/settings/{key}  — get all settings (manager only)
PATCH /api/settings/{key}  — update a setting value (manager only)

The ai_enabled key accepts {value: "true"} or {value: "false"}.
Every update is written to the AuditLog.
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_manager
from app.database import get_db
from app.models import AuditLog, Settings, User

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SettingUpdate(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------

@router.get("/settings")
def get_all_settings(
    current_user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Return all runtime settings as a key→value map (manager only)."""
    rows = db.query(Settings).order_by(Settings.key).all()
    return {
        "settings": [
            {
                "key":        row.key,
                "value":      row.value,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------
# PATCH /api/settings/{key}
# ---------------------------------------------------------------------------

@router.patch("/settings/{key}")
def update_setting(
    key: str,
    payload: SettingUpdate,
    current_user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Update a runtime setting by key (manager only).

    The ai_enabled key accepts only "true" or "false" (case-insensitive).
    All changes are written to the AuditLog.
    """
    row: Optional[Settings] = (
        db.query(Settings).filter(Settings.key == key).first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Setting '{key}' not found",
        )

    # Special validation for ai_enabled
    if key == "ai_enabled":
        normalised = payload.value.strip().lower()
        if normalised not in ("true", "false"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="ai_enabled value must be 'true' or 'false'",
            )
        payload = SettingUpdate(value=normalised)

    old_value = row.value
    row.value = payload.value
    row.updated_at = datetime.utcnow()

    # Audit log
    db.add(AuditLog(
        user_id=current_user.id,
        action="setting_updated",
        details=json.dumps({
            "key":       key,
            "old_value": old_value,
            "new_value": payload.value,
        }),
    ))

    db.commit()
    db.refresh(row)

    return {
        "key":        row.key,
        "value":      row.value,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
