"""
Waste router for SmartCanteen AI.

POST /api/waste         — log waste entry (staff/manager)
GET  /api/waste         — list waste logs with optional filters (manager only)
GET  /api/waste/summary — per-item waste rate + per-day totals (manager only)
"""

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_manager
from app.database import get_db
from app.models import AuditLog, MenuItem, User, UserRole, WasteLog

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class WasteLogIn(BaseModel):
    menu_item_id: int
    date: str                  # ISO date string "YYYY-MM-DD"
    prepared_qty: int
    sold_qty: int
    leftover_qty: int

    @field_validator("prepared_qty", "sold_qty", "leftover_qty")
    @classmethod
    def non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("quantity must be >= 0")
        return v

    @field_validator("date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date must be in YYYY-MM-DD format")
        return v


# ---------------------------------------------------------------------------
# POST /api/waste
# ---------------------------------------------------------------------------

@router.post("/waste", status_code=status.HTTP_201_CREATED)
def log_waste(
    payload: WasteLogIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Log waste for a menu item on a given date. Staff or manager only."""
    if current_user.role not in (UserRole.staff, UserRole.manager):
        raise HTTPException(status_code=403, detail="Staff or manager access required")

    # Validate menu item
    item: Optional[MenuItem] = db.query(MenuItem).filter(
        MenuItem.id == payload.menu_item_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")

    # Parse date
    log_date = datetime.strptime(payload.date, "%Y-%m-%d").date()

    # Create waste log row
    entry = WasteLog(
        menu_item_id=item.id,
        date=log_date,
        prepared_qty=payload.prepared_qty,
        sold_qty=payload.sold_qty,
        leftover_qty=payload.leftover_qty,
        logged_by=current_user.id,
    )
    db.add(entry)

    # Audit log
    db.add(AuditLog(
        user_id=current_user.id,
        action="waste_logged",
        details=json.dumps({
            "menu_item_id": item.id,
            "item_name": item.name,
            "date": payload.date,
            "prepared_qty": payload.prepared_qty,
            "sold_qty": payload.sold_qty,
            "leftover_qty": payload.leftover_qty,
        }),
    ))

    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id,
        "menu_item_id": entry.menu_item_id,
        "item_name": item.name,
        "date": entry.date.isoformat(),
        "prepared_qty": entry.prepared_qty,
        "sold_qty": entry.sold_qty,
        "leftover_qty": entry.leftover_qty,
        "logged_by": current_user.college_id,
        "created_at": entry.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/waste  (manager only)
# ---------------------------------------------------------------------------

@router.get("/waste")
def get_waste_logs(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date:   Optional[str] = Query(None, description="YYYY-MM-DD"),
    item_id:    Optional[int] = Query(None),
    current_user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Return waste log entries with optional date range and item filter."""
    query = db.query(WasteLog)

    if start_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD")
        query = query.filter(WasteLog.date >= sd)

    if end_date:
        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="end_date must be YYYY-MM-DD")
        query = query.filter(WasteLog.date <= ed)

    if item_id is not None:
        query = query.filter(WasteLog.menu_item_id == item_id)

    rows = query.order_by(WasteLog.date.desc(), WasteLog.created_at.desc()).all()

    return [
        {
            "id": r.id,
            "menu_item_id": r.menu_item_id,
            "item_name": r.menu_item.name if r.menu_item else None,
            "date": r.date.isoformat(),
            "prepared_qty": r.prepared_qty,
            "sold_qty": r.sold_qty,
            "leftover_qty": r.leftover_qty,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /api/waste/summary  (manager only)
# ---------------------------------------------------------------------------

@router.get("/waste/summary")
def waste_summary(
    current_user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Return aggregated waste statistics for charts.

    per_item: list of {item_name, total_prepared, total_leftover, waste_rate_pct}
    per_day:  list of {date, total_prepared, total_leftover, waste_rate_pct}
    """
    rows = db.query(WasteLog).all()

    # --- Per-item aggregation ---
    item_totals: dict[int, dict] = defaultdict(
        lambda: {"name": "", "prepared": 0, "leftover": 0}
    )
    for r in rows:
        item_name = r.menu_item.name if r.menu_item else f"item_{r.menu_item_id}"
        item_totals[r.menu_item_id]["name"]     = item_name
        item_totals[r.menu_item_id]["prepared"] += r.prepared_qty
        item_totals[r.menu_item_id]["leftover"] += r.leftover_qty

    per_item = []
    for data in item_totals.values():
        waste_rate = (
            round(100.0 * data["leftover"] / data["prepared"], 1)
            if data["prepared"] > 0
            else 0.0
        )
        per_item.append({
            "item_name":       data["name"],
            "total_prepared":  data["prepared"],
            "total_leftover":  data["leftover"],
            "waste_rate_pct":  waste_rate,
        })
    per_item.sort(key=lambda x: x["waste_rate_pct"], reverse=True)

    # --- Per-day aggregation ---
    day_totals: dict[str, dict] = defaultdict(lambda: {"prepared": 0, "leftover": 0})
    for r in rows:
        key = r.date.isoformat()
        day_totals[key]["prepared"] += r.prepared_qty
        day_totals[key]["leftover"] += r.leftover_qty

    per_day = []
    for day_str, data in sorted(day_totals.items()):
        waste_rate = (
            round(100.0 * data["leftover"] / data["prepared"], 1)
            if data["prepared"] > 0
            else 0.0
        )
        per_day.append({
            "date":           day_str,
            "total_prepared": data["prepared"],
            "total_leftover": data["leftover"],
            "waste_rate_pct": waste_rate,
        })

    return {"per_item": per_item, "per_day": per_day}
