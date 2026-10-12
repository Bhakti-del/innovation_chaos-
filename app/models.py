"""
SQLAlchemy ORM models for SmartCanteen AI.
All models use the shared Base from app.database.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Date,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    student = "student"
    staff = "staff"
    manager = "manager"


class OrderStatus(str, enum.Enum):
    pending = "pending"
    ready = "ready"
    collected = "collected"
    cancelled = "cancelled"


class ModelUsed(str, enum.Enum):
    lightgbm = "lightgbm"
    rolling_avg = "rolling_avg"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    """Registered users: students, counter staff, and managers."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    college_id = Column(String(64), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.student)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")
    waste_logs = relationship("WasteLog", back_populates="logged_by_user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")


class MenuItem(Base):
    """Items available on the canteen menu."""
    __tablename__ = "menu_items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    category = Column(String(64), nullable=True)
    is_available = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    order_items = relationship("OrderItem", back_populates="menu_item")
    waste_logs = relationship("WasteLog", back_populates="menu_item")
    forecast_caches = relationship("ForecastCache", back_populates="item")


class PickupSlot(Base):
    """15-minute pickup windows that students book when ordering."""
    __tablename__ = "pickup_slots"

    id = Column(Integer, primary_key=True, index=True)
    slot_time = Column(DateTime, nullable=False, index=True)
    capacity = Column(Integer, nullable=False, default=30)
    order_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)

    # Relationships
    orders = relationship("Order", back_populates="slot")


class Order(Base):
    """A student's order tied to a specific pickup slot."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    slot_id = Column(Integer, ForeignKey("pickup_slots.id"), nullable=False, index=True)
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.pending)
    total_price = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="orders")
    slot = relationship("PickupSlot", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    """Individual line items within an order."""
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    menu_item_id = Column(Integer, ForeignKey("menu_items.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(Float, nullable=False)

    # Relationships
    order = relationship("Order", back_populates="items")
    menu_item = relationship("MenuItem", back_populates="order_items")


class WasteLog(Base):
    """Daily leftover record per menu item, entered by staff at closing."""
    __tablename__ = "waste_logs"

    id = Column(Integer, primary_key=True, index=True)
    menu_item_id = Column(Integer, ForeignKey("menu_items.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    prepared_qty = Column(Integer, nullable=False, default=0)
    sold_qty = Column(Integer, nullable=False, default=0)
    leftover_qty = Column(Integer, nullable=False, default=0)
    logged_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    menu_item = relationship("MenuItem", back_populates="waste_logs")
    logged_by_user = relationship("User", back_populates="waste_logs")


class AuditLog(Base):
    """Append-only record of every staff/manager action for review."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    action = Column(String(128), nullable=False)
    details = Column(Text, nullable=True)   # JSON string
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="audit_logs")


class Settings(Base):
    """Key-value store for runtime configuration (editable by manager)."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), unique=True, nullable=False, index=True)
    value = Column(String(256), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ForecastCache(Base):
    """Cached model output for a (slot_time, menu_item) pair.
    Invalidated whenever a new order is placed for that slot.
    """
    __tablename__ = "forecast_cache"

    id = Column(Integer, primary_key=True, index=True)
    slot_time = Column(DateTime, nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("menu_items.id"), nullable=False, index=True)
    predicted_qty = Column(Integer, nullable=False)
    low_bound = Column(Integer, nullable=False)
    high_bound = Column(Integer, nullable=False)
    cook_now = Column(Integer, nullable=False)   # portions to cook immediately
    model_used = Column(Enum(ModelUsed), nullable=False, default=ModelUsed.lightgbm)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    item = relationship("MenuItem", back_populates="forecast_caches")
