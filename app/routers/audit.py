"""
Audit router for SmartCanteen AI.

GET /api/audit — paginated list of audit log entries (manager only)
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import require_manager
from app.database import get_db
from app.models import AuditLog, User

router = APIRouter()


@router.get("/audit")
def list_audit_logs(
    page:      int = Query(1,  ge=1,   description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=200, description="Entries per page"),
    current_user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Return paginated audit log entries, newest first.

    Query params:
      page      — 1-indexed page number (default 1)
      page_size — entries per page (default 20, max 200)
    """
    total = db.query(AuditLog).count()
    offset = (page - 1) * page_size

    rows = (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "entries": [
            {
                "id":         row.id,
                "user_id":    row.user_id,
                "college_id": row.user.college_id if row.user else None,
                "action":     row.action,
                "details":    row.details,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }
