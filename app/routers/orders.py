"""
Orders router for SmartCanteen AI.

POST   /api/orders           — place order (student only)
GET    /api/orders/my        — student's own orders
GET    /api/orders           — all orders (staff/manager only)
PATCH  /api/orders/{id}/status — update order status (staff/manager only)
GET    /api/slots            — today's slots with occupancy
GET    /api/menu             — available menu items (public)
"""

import json
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_manager, require_staff, require_student
from app.database import get_db
from app.models import (
    AuditLog,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    PickupSlot,
    Settings,
    User,
    UserRole,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class OrderLineIn(BaseModel):
    menu_item_id: int
    quantity: int

    @field_validator("quantity")
    @classmethod
    def qty_range(cls, v: int) -> int:
        if v < 1 or v > 10:
            raise ValueError("quantity must be between 1 and 10")
        return v


class OrderIn(BaseModel):
    slot_id: int
    items: list[OrderLineIn]

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v):
        if not v:
            raise ValueError("order must contain at least one item")
        return v


class StatusUpdateIn(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.pending:   {OrderStatus.ready, OrderStatus.cancelled},
    OrderStatus.ready:     {OrderStatus.collected, OrderStatus.cancelled},
    OrderStatus.collected: {OrderStatus.cancelled},
    OrderStatus.cancelled: set(),
}


def _get_setting_float(db: Session, key: str, default: float) -> float:
    row = db.query(Settings).filter(Settings.key == key).first()
    try:
        return float(row.value) if row else default
    except (ValueError, TypeError):
        return default


def _wait_time_minutes(open_orders: int, throughput: float) -> float:
    """Estimate wait time given open orders ahead and counter throughput."""
    if throughput <= 0:
        return 0.0
    return open_orders / throughput


def _audit(db: Session, user: User, action: str, details: dict) -> None:
    db.add(AuditLog(user_id=user.id, action=action, details=json.dumps(details)))


# ---------------------------------------------------------------------------
# GET /api/menu  (public)
# ---------------------------------------------------------------------------

@router.get("/menu")
def list_menu(db: Session = Depends(get_db)):
    """Return all available (is_available=True) menu items. No auth required."""
    items = db.query(MenuItem).filter(MenuItem.is_available == True).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "price": item.price,
            "category": item.category,
        }
        for item in items
    ]


# ---------------------------------------------------------------------------
# GET /api/slots
# ---------------------------------------------------------------------------

@router.get("/slots")
def list_slots(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return today's pickup slots with current occupancy."""
    today = date.today()
    start = datetime(today.year, today.month, today.day, 0, 0, 0)
    end   = datetime(today.year, today.month, today.day, 23, 59, 59)

    slots = (
        db.query(PickupSlot)
        .filter(PickupSlot.slot_time >= start, PickupSlot.slot_time <= end)
        .order_by(PickupSlot.slot_time)
        .all()
    )
    return [
        {
            "id": s.id,
            "slot_time": s.slot_time.isoformat(),
            "capacity": s.capacity,
            "order_count": s.order_count,
            "is_active": s.is_active,
            "available": max(0, s.capacity - s.order_count),
        }
        for s in slots
    ]


# ---------------------------------------------------------------------------
# POST /api/orders
# ---------------------------------------------------------------------------

@router.post("/orders", status_code=status.HTTP_201_CREATED)
def place_order(
    payload: OrderIn,
    current_user: User = Depends(require_student),
    db: Session = Depends(get_db),
):
    """Place a pre-order for a pickup slot (student only).

    Validates:
    - Slot exists, is active, and is not full.
    - Each menu item exists and is available.
    - Each line quantity ≤ 10.
    - Student has no other active (non-cancelled) order for this slot.

    Returns the created order plus wait_time_minutes.
    If wait > threshold, also suggests the next quieter slot.
    """
    # --- Load and validate slot ---
    slot: Optional[PickupSlot] = db.query(PickupSlot).filter(
        PickupSlot.id == payload.slot_id
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Pickup slot not found")
    if not slot.is_active:
        raise HTTPException(status_code=400, detail="Pickup slot is not active")
    if slot.order_count >= slot.capacity:
        raise HTTPException(status_code=400, detail="Pickup slot is full")

    # --- One active order per student per slot ---
    existing_order = (
        db.query(Order)
        .filter(
            Order.user_id == current_user.id,
            Order.slot_id == payload.slot_id,
            Order.status != OrderStatus.cancelled,
        )
        .first()
    )
    if existing_order:
        raise HTTPException(
            status_code=409,
            detail="You already have an active order for this slot",
        )

    # --- Validate and price each line item ---
    total_price = 0.0
    resolved_items: list[tuple[MenuItem, int]] = []

    for line in payload.items:
        menu_item: Optional[MenuItem] = db.query(MenuItem).filter(
            MenuItem.id == line.menu_item_id
        ).first()
        if not menu_item:
            raise HTTPException(
                status_code=404,
                detail=f"Menu item {line.menu_item_id} not found",
            )
        if not menu_item.is_available:
            raise HTTPException(
                status_code=400,
                detail=f"'{menu_item.name}' is currently unavailable",
            )
        total_price += menu_item.price * line.quantity
        resolved_items.append((menu_item, line.quantity))

    # --- Create Order ---
    order = Order(
        user_id=current_user.id,
        slot_id=slot.id,
        status=OrderStatus.pending,
        total_price=total_price,
    )
    db.add(order)

    # Update slot occupancy
    slot.order_count += 1
    db.flush()  # get order.id

    # --- Create OrderItems ---
    for menu_item, qty in resolved_items:
        db.add(OrderItem(
            order_id=order.id,
            menu_item_id=menu_item.id,
            quantity=qty,
            unit_price=menu_item.price,
        ))

    db.commit()
    db.refresh(order)

    # --- Compute wait time ---
    throughput = _get_setting_float(db, "counter_throughput_per_minute", 2.0)
    threshold  = _get_setting_float(db, "wait_time_threshold_minutes", 5.0)
    wait_mins  = _wait_time_minutes(slot.order_count, throughput)

    response: dict = {
        "order_id": order.id,
        "slot_id": slot.id,
        "slot_time": slot.slot_time.isoformat(),
        "status": order.status.value,
        "total_price": order.total_price,
        "wait_time_minutes": round(wait_mins, 1),
        "items": [
            {
                "menu_item_id": mi.id,
                "name": mi.name,
                "quantity": qty,
                "unit_price": mi.price,
            }
            for mi, qty in resolved_items
        ],
    }

    # --- Suggest a quieter slot if wait exceeds threshold ---
    if wait_mins > threshold:
        today = date.today()
        start = datetime(today.year, today.month, today.day, 0, 0, 0)
        future_slots = (
            db.query(PickupSlot)
            .filter(
                PickupSlot.slot_time > slot.slot_time,
                PickupSlot.slot_time >= start,
                PickupSlot.is_active == True,
                PickupSlot.order_count < PickupSlot.capacity,
            )
            .order_by(PickupSlot.order_count.asc(), PickupSlot.slot_time.asc())
            .first()
        )
        if future_slots:
            suggested_wait = _wait_time_minutes(future_slots.order_count, throughput)
            response["suggested_slot_id"]   = future_slots.id
            response["suggested_slot_time"] = future_slots.slot_time.isoformat()
            response["wait_at_suggested"]   = round(suggested_wait, 1)

    return response


# ---------------------------------------------------------------------------
# GET /api/orders/my
# ---------------------------------------------------------------------------

@router.get("/orders/my")
def my_orders(
    current_user: User = Depends(require_student),
    db: Session = Depends(get_db),
):
    """Return all orders belonging to the authenticated student."""
    orders = (
        db.query(Order)
        .filter(Order.user_id == current_user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


# ---------------------------------------------------------------------------
# GET /api/orders  (staff/manager)
# ---------------------------------------------------------------------------

@router.get("/orders")
def all_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all orders. Requires staff or manager role."""
    if current_user.role not in (UserRole.staff, UserRole.manager):
        raise HTTPException(status_code=403, detail="Staff or manager access required")

    orders = db.query(Order).order_by(Order.created_at.desc()).all()
    return [_serialize_order(o) for o in orders]


# ---------------------------------------------------------------------------
# PATCH /api/orders/{id}/status  (staff/manager)
# ---------------------------------------------------------------------------

@router.patch("/orders/{order_id}/status")
def update_order_status(
    order_id: int,
    payload: StatusUpdateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update order status. Staff/manager only.

    Allowed transitions:
      pending   → ready | cancelled
      ready     → collected | cancelled
      collected → cancelled
    """
    if current_user.role not in (UserRole.staff, UserRole.manager):
        raise HTTPException(status_code=403, detail="Staff or manager access required")

    order: Optional[Order] = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Validate target status
    try:
        new_status = OrderStatus(payload.status.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown status '{payload.status}'. "
                   f"Valid values: {[s.value for s in OrderStatus]}",
        )

    allowed = _ALLOWED_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition order from '{order.status.value}' to '{new_status.value}'",
        )

    old_status = order.status.value
    order.status = new_status

    # Free up slot capacity on cancellation
    if new_status == OrderStatus.cancelled and old_status != OrderStatus.cancelled.value:
        slot = db.query(PickupSlot).filter(PickupSlot.id == order.slot_id).first()
        if slot and slot.order_count > 0:
            slot.order_count -= 1

    _audit(db, current_user, "order_status_update", {
        "order_id": order_id,
        "from": old_status,
        "to": new_status.value,
    })
    db.commit()
    db.refresh(order)

    return _serialize_order(order)


# ---------------------------------------------------------------------------
# Serialiser helper
# ---------------------------------------------------------------------------

def _serialize_order(order: Order) -> dict:
    return {
        "id": order.id,
        "user_id": order.user_id,
        "slot_id": order.slot_id,
        "slot_time": order.slot.slot_time.isoformat() if order.slot else None,
        "status": order.status.value,
        "total_price": order.total_price,
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "menu_item_id": oi.menu_item_id,
                "name": oi.menu_item.name if oi.menu_item else None,
                "quantity": oi.quantity,
                "unit_price": oi.unit_price,
            }
            for oi in order.items
        ],
    }
