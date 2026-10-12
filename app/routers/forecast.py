"""
Forecast router for SmartCanteen AI.

GET  /api/forecast         — prep plan for next 4 slots from now (authenticated)
POST /api/forecast/retrain — trigger weekly retraining (manager only)
"""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_manager
from app.database import get_db
from app.models import MenuItem, Order, OrderItem, OrderStatus, PickupSlot, Settings, User

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent.parent  # project root


def _get_setting_bool(db: Session, key: str, default: bool) -> bool:
    from app.models import Settings  # local import avoids circular
    row = db.query(Settings).filter(Settings.key == key).first()
    if row is None:
        return default
    return row.value.lower() in ("true", "1", "yes")


def _next_slots(db: Session, n: int = 4) -> list[PickupSlot]:
    """Return up to *n* PickupSlot rows whose slot_time is >= now."""
    now = datetime.utcnow()
    return (
        db.query(PickupSlot)
        .filter(PickupSlot.slot_time >= now, PickupSlot.is_active == True)
        .order_by(PickupSlot.slot_time)
        .limit(n)
        .all()
    )


def _pre_order_counts_for_slot(db: Session, slot_id: int) -> dict[str, int]:
    """Return a dict {item_name: total_qty} for active orders in *slot_id*."""
    rows = (
        db.query(MenuItem.name, OrderItem.quantity)
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            Order.slot_id == slot_id,
            Order.status != OrderStatus.cancelled,
        )
        .all()
    )
    counts: dict[str, int] = {}
    for name, qty in rows:
        counts[name] = counts.get(name, 0) + qty
    return counts


# ---------------------------------------------------------------------------
# GET /api/forecast
# ---------------------------------------------------------------------------

@router.get("/forecast")
def get_forecast_plan(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the prep plan for the next 4 pickup slots.

    Each slot contains a list of items with:
      name, predicted, low, high, cook_now, model_used
    """
    ai_enabled = _get_setting_bool(db, "ai_enabled", True)
    slots = _next_slots(db, n=4)

    if not slots:
        return {"slots": [], "message": "No upcoming slots found for today"}

    # Import predict lazily — avoids loading the 2.3 MB model at import time
    try:
        from ml.predict import get_forecast
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Forecast module unavailable: {exc}",
        )

    result = []
    for slot in slots:
        pre_orders = _pre_order_counts_for_slot(db, slot.id)
        try:
            items = get_forecast(
                slot_time=slot.slot_time,
                pre_order_counts=pre_orders,
                ai_enabled=ai_enabled,
                db=db,
            )
        except Exception as exc:
            # Graceful degradation: report the slot but skip its items
            result.append({
                "slot_id": slot.id,
                "slot_time": slot.slot_time.isoformat(),
                "error": str(exc),
                "items": [],
            })
            continue

        result.append({
            "slot_id": slot.id,
            "slot_time": slot.slot_time.isoformat(),
            "order_count": slot.order_count,
            "capacity": slot.capacity,
            "items": [
                {
                    "name": item["item_name"],
                    "predicted": item["predicted"],
                    "low": item["low"],
                    "high": item["high"],
                    "cook_now": item["cook_now"],
                    "model_used": item["model_used"],
                }
                for item in items
            ],
        })

    return {"slots": result, "ai_enabled": ai_enabled}


# ---------------------------------------------------------------------------
# POST /api/forecast/retrain  (manager only)
# ---------------------------------------------------------------------------

@router.post("/forecast/retrain", status_code=status.HTTP_202_ACCEPTED)
def trigger_retrain(
    current_user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Trigger a background retraining job (manager only).

    Launches ml/train.py as a non-blocking subprocess.
    Returns immediately with status "started".
    """
    train_script = _ROOT / "ml" / "train.py"
    if not train_script.exists():
        raise HTTPException(status_code=500, detail="Training script not found")

    try:
        subprocess.Popen(
            [sys.executable, str(train_script)],
            cwd=str(_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start retraining: {exc}")

    # Audit log
    import json
    from app.models import AuditLog
    db.add(AuditLog(
        user_id=current_user.id,
        action="forecast_retrain_triggered",
        details=json.dumps({"script": str(train_script)}),
    ))
    db.commit()

    return {"status": "started", "message": "Retraining job launched in background"}
