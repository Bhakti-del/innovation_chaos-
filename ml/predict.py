"""
SmartCanteen AI — Inference module.

Public API:

    from ml.predict import get_forecast

    forecast = get_forecast(
        slot_time=datetime(2026, 1, 12, 12, 30),
        pre_order_counts={"Veg Biryani": 8, "Masala Dosa": 3},
        ai_enabled=True,
        db=db_session,          # SQLAlchemy session (used for future DB-based
                                # pre-order queries; pass None if not available)
    )

Each returned dict has the shape:
    {
        "item_name":  str,
        "predicted":  float,   # central estimate (portions)
        "low":        float,   # 10th-percentile bound
        "high":       float,   # 90th-percentile bound
        "cook_now":   int,     # max(low * 1.1, 10) rounded to nearest 10
        "model_used": str,     # "lightgbm" | "rolling_avg"
    }
"""

import logging
import os
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE       = Path(__file__).resolve().parent
ROOT        = _HERE.parent
MODEL_PATH  = _HERE / "model.pkl"
LE_PATH     = _HERE / "label_encoder.pkl"
DATA_PATH   = ROOT  / "data" / "sales_data.csv"

# Canteen opens at 08:00; each slot is 15 minutes.
_CANTEEN_OPEN_MINUTES = 8 * 60   # 480

# Inference SLA (milliseconds)
_INFERENCE_SLA_MS = 200

ITEMS = [
    "Veg Biryani",
    "Masala Dosa",
    "Paneer Wrap",
    "Samosa",
    "Chai",
    "Chole Bhature",
    "Idli Sambar",
    "Pasta",
]

# ---------------------------------------------------------------------------
# Lazy-loaded singletons
# ---------------------------------------------------------------------------

_model_cache: Optional[dict]  = None
_le_cache                     = None
_rolling_cache: Optional[pd.DataFrame] = None


def _load_model() -> Optional[dict]:
    """Load the model artefact from disk (cached after first load)."""
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    if not MODEL_PATH.exists():
        return None
    try:
        import joblib
        _model_cache = joblib.load(MODEL_PATH)
        logger.info("LightGBM model loaded from %s", MODEL_PATH)
        return _model_cache
    except Exception as exc:
        logger.warning("Failed to load model from %s: %s", MODEL_PATH, exc)
        return None


def _load_label_encoder():
    """Load the LabelEncoder from disk (cached after first load)."""
    global _le_cache
    if _le_cache is not None:
        return _le_cache
    if not LE_PATH.exists():
        return None
    try:
        import joblib
        _le_cache = joblib.load(LE_PATH)
        return _le_cache
    except Exception as exc:
        logger.warning("Failed to load label encoder from %s: %s", LE_PATH, exc)
        return None


def _load_rolling_data() -> Optional[pd.DataFrame]:
    """Load and cache the rolling_7d_avg lookup table from sales_data.csv."""
    global _rolling_cache
    if _rolling_cache is not None:
        return _rolling_cache
    if not DATA_PATH.exists():
        logger.warning("sales_data.csv not found at %s — rolling-avg fallback unavailable", DATA_PATH)
        return None
    try:
        df = pd.read_csv(DATA_PATH, parse_dates=["date"])
        # Keep only the columns we need for the fallback
        _rolling_cache = df[["day_of_week", "slot_index", "item_name", "rolling_7d_avg"]].copy()
        return _rolling_cache
    except Exception as exc:
        logger.warning("Failed to load rolling data: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_index(dt: datetime) -> int:
    """Convert a datetime to a 15-minute slot index (canteen opens 08:00)."""
    minutes = dt.hour * 60 + dt.minute
    return max(0, (minutes - _CANTEEN_OPEN_MINUTES) // 15)


def _round_to_nearest_10(x: float) -> int:
    return max(10, int(round(x / 10.0) * 10))


def _cook_now(low: float) -> int:
    """cook_now = max(low * 1.1, 10) rounded to the nearest 10."""
    return _round_to_nearest_10(max(low * 1.1, 10.0))


# ---------------------------------------------------------------------------
# LightGBM path
# ---------------------------------------------------------------------------

def _forecast_lightgbm(
    slot_time: datetime,
    pre_order_counts: dict,
    artefact: dict,
    le,
) -> list[dict]:
    """Run LightGBM inference for all items for the given slot."""
    weekday    = slot_time.weekday()
    slot_idx   = _slot_index(slot_time)
    is_exam    = 0   # Runtime flag; callers can extend if needed
    is_event   = 0
    is_holiday = 0

    main_model      = artefact["main_model"]
    quantile_models = artefact["quantile_models"]

    results = []
    for item in ITEMS:
        # Encode item
        try:
            item_code = int(le.transform([item])[0])
        except Exception:
            item_code = 0

        # Look up rolling_7d_avg from history (best available estimate)
        rolling_avg = _get_rolling_avg(weekday, slot_idx, item)

        X = pd.DataFrame([{
            "day_of_week":  weekday,
            "slot_index":   slot_idx,
            "is_exam_day":  is_exam,
            "is_event_day": is_event,
            "is_holiday":   is_holiday,
            "rolling_7d_avg": rolling_avg,
            "item_encoded": item_code,
        }])

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            predicted = float(np.clip(main_model.predict(X)[0], 0, None))

        # Pre-order adjustment: add confirmed pre-orders on top of prediction
        pre_orders = pre_order_counts.get(item, 0)
        predicted  = max(predicted, float(pre_orders))

        # Quantile bounds
        q_models = quantile_models.get(item)
        if q_models:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                low  = float(np.clip(q_models["q10"].predict(X)[0], 0, None))
                high = float(np.clip(q_models["q90"].predict(X)[0], 0, None))
            # Ensure bounds are sensible relative to the central prediction
            low  = min(low,  predicted)
            high = max(high, predicted)
        else:
            # Fallback: ±15% of predicted
            low  = predicted * 0.85
            high = predicted * 1.15

        results.append({
            "item_name":  item,
            "predicted":  round(predicted, 1),
            "low":        round(low, 1),
            "high":       round(high, 1),
            "cook_now":   _cook_now(low),
            "model_used": "lightgbm",
        })

    return results


# ---------------------------------------------------------------------------
# Rolling-average fallback path
# ---------------------------------------------------------------------------

def _get_rolling_avg(weekday: int, slot_idx: int, item: str) -> float:
    """Return the latest rolling_7d_avg for this (weekday, slot, item) or 0."""
    df = _load_rolling_data()
    if df is None:
        return 0.0
    mask = (
        (df["day_of_week"] == weekday) &
        (df["slot_index"]  == slot_idx) &
        (df["item_name"]   == item)
    )
    subset = df[mask]["rolling_7d_avg"].dropna()
    if subset.empty:
        return 0.0
    return float(subset.iloc[-1])


def _forecast_rolling_avg(
    slot_time: datetime,
    pre_order_counts: dict,
) -> list[dict]:
    """Fallback: use rolling_7d_avg × 1.1 buffer for every item."""
    weekday  = slot_time.weekday()
    slot_idx = _slot_index(slot_time)

    results = []
    for item in ITEMS:
        avg = _get_rolling_avg(weekday, slot_idx, item)

        # If no history at all, use a safe minimum
        if avg == 0.0:
            avg = 5.0

        predicted = avg * 1.1
        predicted = max(predicted, float(pre_order_counts.get(item, 0)))

        low  = predicted * 0.85
        high = predicted * 1.15

        results.append({
            "item_name":  item,
            "predicted":  round(predicted, 1),
            "low":        round(low, 1),
            "high":       round(high, 1),
            "cook_now":   _cook_now(low),
            "model_used": "rolling_avg",
        })

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_forecast(
    slot_time: datetime,
    pre_order_counts: dict,
    ai_enabled: bool,
    db,  # SQLAlchemy session — reserved for future DB-sourced pre-orders
) -> list[dict]:
    """Return a forecast for every canteen item for the given 15-minute slot.

    Parameters
    ----------
    slot_time:
        Start of the slot to forecast (only hour/minute/weekday are used).
    pre_order_counts:
        Dict mapping item_name → confirmed pre-order count for this slot.
        These are added on top of the model's statistical prediction.
    ai_enabled:
        If False, or if the model file is missing, the rolling-average
        fallback is used regardless.
    db:
        SQLAlchemy session.  Currently unused but reserved so callers can
        pass it without changes when DB-sourced data is added.

    Returns
    -------
    List of dicts with keys: item_name, predicted, low, high, cook_now,
    model_used.
    """
    t_start = time.perf_counter()

    try:
        if ai_enabled:
            artefact = _load_model()
            le       = _load_label_encoder()
            if artefact is not None and le is not None:
                results = _forecast_lightgbm(slot_time, pre_order_counts, artefact, le)
            else:
                logger.warning(
                    "ai_enabled=True but model/encoder not found — falling back to rolling_avg"
                )
                results = _forecast_rolling_avg(slot_time, pre_order_counts)
        else:
            results = _forecast_rolling_avg(slot_time, pre_order_counts)

    except Exception as exc:
        logger.error("Forecast error (%s) — using rolling_avg fallback: %s",
                     type(exc).__name__, exc)
        results = _forecast_rolling_avg(slot_time, pre_order_counts)

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    if elapsed_ms > _INFERENCE_SLA_MS:
        logger.warning(
            "Inference took %.1f ms — exceeds %d ms SLA", elapsed_ms, _INFERENCE_SLA_MS
        )
    else:
        logger.debug("Inference completed in %.1f ms", elapsed_ms)

    return results


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    test_slot = datetime(2026, 1, 12, 12, 30)   # Monday 12:30

    print("\n--- LightGBM forecast (ai_enabled=True) ---")
    forecast_ai = get_forecast(
        slot_time=test_slot,
        pre_order_counts={"Veg Biryani": 5, "Masala Dosa": 2},
        ai_enabled=True,
        db=None,
    )
    for row in forecast_ai:
        print(f"  {row['item_name']:<18} predicted={row['predicted']:5.1f}  "
              f"[{row['low']:5.1f} – {row['high']:5.1f}]  "
              f"cook_now={row['cook_now']:3d}  ({row['model_used']})")

    print("\n--- Rolling-average fallback (ai_enabled=False) ---")
    forecast_fallback = get_forecast(
        slot_time=test_slot,
        pre_order_counts={},
        ai_enabled=False,
        db=None,
    )
    for row in forecast_fallback:
        print(f"  {row['item_name']:<18} predicted={row['predicted']:5.1f}  "
              f"[{row['low']:5.1f} – {row['high']:5.1f}]  "
              f"cook_now={row['cook_now']:3d}  ({row['model_used']})")
