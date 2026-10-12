"""
Synthetic sales data generator for SmartCanteen AI.

Generates 6 weeks of per-item, per-15-minute-slot sales data and saves it to
data/sales_data.csv.  Run from the project root:

    python data/generate.py
"""

import os
import numpy as np
import pandas as pd
from datetime import date, timedelta, time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RANDOM_SEED = 42
rng = np.random.default_rng(RANDOM_SEED)

# Start on a Monday so weeks are aligned Monday–Sunday
START_DATE = date(2026, 1, 5)  # Week 1 starts here
NUM_WEEKS = 6

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

# Base demand at the lunch peak (12:00–13:30).  All other times are scaled down.
BASE_LUNCH = {
    "Veg Biryani":   70,
    "Masala Dosa":   40,
    "Paneer Wrap":   25,
    "Samosa":        50,
    "Chai":          80,
    "Chole Bhature": 35,
    "Idli Sambar":   45,
    "Pasta":         20,
}

# Tea-time peak is 40 % of the lunch base
TEA_FRACTION = 0.40

# Near-zero outside peaks (≈5 % of lunch base, still plausible walk-ins)
OTHER_FRACTION = 0.05

# Canteen opens 08:00, closes 18:00 → 40 slots of 15 min each
CANTEEN_OPEN  = time(8, 0)
CANTEEN_CLOSE = time(18, 0)

# Lunch peak slots: 12:00–13:30  (slot indices for a canteen opening at 08:00)
# Slot index = (hour*60 + minute - 480) // 15
def _slot_index(h: int, m: int) -> int:
    return (h * 60 + m - 480) // 15

LUNCH_SLOTS = set(range(_slot_index(12, 0), _slot_index(13, 30) + 1))   # indices 16–22
TEA_SLOTS   = set(range(_slot_index(16, 0), _slot_index(16, 30) + 1))   # indices 32–34

TOTAL_SLOTS = (_slot_index(18, 0))  # slot index just past closing = 40

# Monday and Friday multiplier
MON_FRI_MULTIPLIER = 1.12

# Exam-day rules: weeks 3 and 5, day-of-week indices 1 (Tue) and 3 (Thu)
# We treat day_of_week as 0=Mon…6=Sun (Python weekday())
EXAM_WEEK_DAYS = {(3, 1), (3, 3), (5, 1), (5, 3)}  # (week_number, weekday)

EXAM_QUICK_MULT  = 1.20   # Paneer Wrap, Masala Dosa
EXAM_BIRYANI_MULT = 0.85

NOISE_SIGMA_FRAC = 0.08   # 8% of mean


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slot_time_str(slot_idx: int) -> str:
    """Return HH:MM string for the start of a slot (canteen opens 08:00)."""
    total_minutes = 480 + slot_idx * 15
    h, m = divmod(total_minutes, 60)
    return f"{h:02d}:{m:02d}"


def demand_multiplier_for_slot(slot_idx: int) -> float:
    if slot_idx in LUNCH_SLOTS:
        return 1.0
    if slot_idx in TEA_SLOTS:
        return TEA_FRACTION
    return OTHER_FRACTION


def base_portions(item: str, slot_idx: int,
                  weekday: int, week_num: int) -> float:
    """Return the expected (noise-free) portions for one cell."""
    base = BASE_LUNCH[item] * demand_multiplier_for_slot(slot_idx)

    # Mon / Fri boost
    if weekday in (0, 4):
        base *= MON_FRI_MULTIPLIER

    # Exam-day adjustments
    is_exam = (week_num, weekday) in EXAM_WEEK_DAYS
    if is_exam:
        if item in ("Masala Dosa", "Paneer Wrap"):
            base *= EXAM_QUICK_MULT
        elif item == "Veg Biryani":
            base *= EXAM_BIRYANI_MULT

    return base


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

def generate() -> pd.DataFrame:
    rows = []

    for week in range(1, NUM_WEEKS + 1):
        week_start = START_DATE + timedelta(weeks=week - 1)

        # One row per day × slot × item
        for day_offset in range(7):
            current_date = week_start + timedelta(days=day_offset)
            weekday = current_date.weekday()  # 0=Mon…6=Sun

            is_exam    = int((week, weekday) in EXAM_WEEK_DAYS)
            is_event   = 0   # No events in synthetic data; can be overridden
            is_holiday = 0   # No holidays; canteen is open all days

            for slot_idx in range(TOTAL_SLOTS):
                slot_str = slot_time_str(slot_idx)

                for item in ITEMS:
                    mean = base_portions(item, slot_idx, weekday, week)
                    noise = rng.normal(0, NOISE_SIGMA_FRAC * max(mean, 1))
                    portions = max(0.0, mean + noise)

                    rows.append({
                        "date":        current_date.isoformat(),
                        "day_of_week": weekday,
                        "slot_index":  slot_idx,
                        "slot_time":   slot_str,
                        "item_name":   item,
                        "is_exam_day": is_exam,
                        "is_event_day": is_event,
                        "is_holiday":  is_holiday,
                        "portions_sold": round(portions, 2),
                    })

    df = pd.DataFrame(rows)

    # Ensure correct types
    df["date"]        = pd.to_datetime(df["date"])
    df["portions_sold"] = df["portions_sold"].astype(float)

    # -----------------------------------------------------------------------
    # Rolling 7-day average per item per slot
    # -----------------------------------------------------------------------
    # For each (item_name, slot_index) pair, compute the rolling 7-day mean
    # of portions_sold across calendar days, then map it back onto every row.
    #
    # Because there is exactly one row per (date, slot_index, item_name) in the
    # raw data, we can do this with a sorted groupby + transform.

    df = df.sort_values(["item_name", "slot_index", "date"]).reset_index(drop=True)

    def _rolling_mean(s: pd.Series) -> pd.Series:
        return s.rolling(window=7, min_periods=1).mean()

    df["rolling_7d_avg"] = (
        df.groupby(["item_name", "slot_index"], group_keys=False)["portions_sold"]
          .transform(_rolling_mean)
          .round(2)
    )

    # Final column order
    df = df[[
        "date", "day_of_week", "slot_index", "slot_time",
        "item_name", "is_exam_day", "is_event_day", "is_holiday",
        "portions_sold", "rolling_7d_avg",
    ]]
    df = df.sort_values(["date", "slot_index", "item_name"]).reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ensure output directory exists (it always should in the scaffold)
    os.makedirs(os.path.dirname(os.path.abspath(__file__)), exist_ok=True)

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sales_data.csv")

    print("Generating synthetic sales data …")
    df = generate()

    df.to_csv(output_path, index=False)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\n{'='*55}")
    print(f"  SmartCanteen AI — Synthetic Data Generator")
    print(f"{'='*55}")
    print(f"  Total rows   : {len(df):,}")
    print(f"  Date range   : {df['date'].min().date()}  →  {df['date'].max().date()}")
    print(f"  Items        : {df['item_name'].nunique()} ({', '.join(sorted(df['item_name'].unique()))})")
    print(f"  Slots/day    : {df['slot_index'].nunique()}")
    print(f"  Days         : {df['date'].nunique()}")
    print(f"  Exam days    : {df[df['is_exam_day']==1]['date'].dt.date.nunique()} unique dates")
    print(f"  Saved to     : {output_path}")
    print(f"\n  Sample rows (lunch peak, week 1):")
    sample = df[
        (df["slot_index"].isin(LUNCH_SLOTS)) &
        (df["date"] == df["date"].min())
    ].head(8)
    print(sample.to_string(index=False))
    print(f"{'='*55}\n")
