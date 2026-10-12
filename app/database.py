"""
Database engine, session factory, and Base for SmartCanteen AI.

Call init_db() once at application startup to create all tables and seed
the database with default users, menu items, pickup slots, and settings.
"""

from datetime import datetime, date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./smartcanteen.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Database initialisation and seeding
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables and seed the database with default data.

    Safe to call on every startup: existence checks prevent duplicate rows.
    """
    # Import models here (after Base is defined) to avoid circular imports.
    from app.models import (  # noqa: PLC0415
        AuditLog,
        ForecastCache,
        MenuItem,
        Order,
        OrderItem,
        PickupSlot,
        Settings,
        User,
        UserRole,
        WasteLog,
    )
    from app.auth import hash_password  # noqa: PLC0415

    # Create tables (no-op if they already exist)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        _seed_users(db, hash_password)
        _seed_menu_items(db)
        _seed_settings(db)
        _seed_pickup_slots(db)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_users(db, hash_password) -> None:
    """Insert default users if they don't already exist."""
    from app.models import User, UserRole  # noqa: PLC0415

    seed_users = [
        {"college_id": "admin",    "password": "admin123",   "role": UserRole.manager},
        {"college_id": "staff1",   "password": "staff123",   "role": UserRole.staff},
        {"college_id": "student1", "password": "student123", "role": UserRole.student},
    ]

    for u in seed_users:
        exists = db.query(User).filter(User.college_id == u["college_id"]).first()
        if not exists:
            db.add(User(
                college_id=u["college_id"],
                hashed_password=hash_password(u["password"]),
                role=u["role"],
                created_at=datetime.utcnow(),
            ))


def _seed_menu_items(db) -> None:
    """Insert the 8 default menu items if none exist yet."""
    from app.models import MenuItem  # noqa: PLC0415

    if db.query(MenuItem).count() > 0:
        return

    items = [
        {"name": "Veg Biryani",     "price": 120, "category": "Main Course"},
        {"name": "Masala Dosa",     "price": 60,  "category": "Breakfast"},
        {"name": "Paneer Wrap",     "price": 80,  "category": "Snacks"},
        {"name": "Samosa",          "price": 20,  "category": "Snacks"},
        {"name": "Chai",            "price": 15,  "category": "Beverages"},
        {"name": "Chole Bhature",   "price": 90,  "category": "Main Course"},
        {"name": "Idli Sambar",     "price": 50,  "category": "Breakfast"},
        {"name": "Pasta",           "price": 75,  "category": "Snacks"},
    ]

    for item in items:
        db.add(MenuItem(
            name=item["name"],
            description=None,
            price=item["price"],
            category=item["category"],
            is_available=True,
            created_at=datetime.utcnow(),
        ))


def _seed_settings(db) -> None:
    """Insert default runtime settings if they don't already exist."""
    from app.models import Settings  # noqa: PLC0415

    defaults = {
        "wait_time_threshold_minutes": "5",
        "counter_throughput_per_minute": "2",
        "ai_enabled": "true",
        "leftover_discount_active": "false",
    }

    for key, value in defaults.items():
        exists = db.query(Settings).filter(Settings.key == key).first()
        if not exists:
            db.add(Settings(
                key=key,
                value=value,
                updated_at=datetime.utcnow(),
            ))


def _seed_pickup_slots(db) -> None:
    """Create 20 pickup slots for today from 11:00 to 15:45 in 15-min steps.

    Skips seeding if slots already exist for today to avoid duplicates on
    restart.
    """
    from app.models import PickupSlot  # noqa: PLC0415

    today = date.today()
    # Check whether slots already exist for today
    start_of_day = datetime(today.year, today.month, today.day, 0, 0, 0)
    end_of_day   = datetime(today.year, today.month, today.day, 23, 59, 59)
    existing = (
        db.query(PickupSlot)
        .filter(PickupSlot.slot_time >= start_of_day)
        .filter(PickupSlot.slot_time <= end_of_day)
        .count()
    )
    if existing > 0:
        return

    # 11:00 → 15:45  →  20 slots  (5 hours = 300 min / 15 = 20)
    base = datetime(today.year, today.month, today.day, 11, 0, 0)
    for i in range(20):
        slot_time = base + timedelta(minutes=15 * i)
        db.add(PickupSlot(
            slot_time=slot_time,
            capacity=30,
            order_count=0,
            is_active=True,
        ))
