"""
simulate.py  -  Generate a synthetic year of POS order data.

This stands in for a real POS export until your friend has real data.
It produces ONE ROW PER ORDER, exactly like a POS "transactions" export:

    order_id, datetime, day_of_week, item, quantity, unit_price, total

Demand is a non-homogeneous Poisson process:
  * Each day has a total volume that depends on weekday/weekend/holiday
    (with random noise) so the YEARLY average lands near `avg_daily`.
  * Within a day, arrivals follow a lunch peak + dinner peak shape.

Usage:
    python simulate.py                      # writes data/pos_orders.csv (1 year)
    python simulate.py --days 365 --avg 250 --seed 7 --out data/pos_orders.csv
"""
from __future__ import annotations
import argparse
import os
import numpy as np
import pandas as pd

from config import Config, add_config_args, resolve_config


# --- Shape of a day: relative demand weight per minute since open ----------
def intraday_weights(open_minutes: int) -> np.ndarray:
    """Two humps: lunch (~12:15) and dinner (~19:00), on a low all-day base."""
    m = np.arange(open_minutes)
    base = 0.25
    lunch = 1.00 * np.exp(-((m - 75) / 28) ** 2)    # peak ~12:15
    dinner = 1.30 * np.exp(-((m - 480) / 50) ** 2)   # peak ~19:00
    afternoon = 0.35 * np.exp(-((m - 270) / 90) ** 2)  # gentle 15:30 lull-fill
    w = base + lunch + dinner + afternoon
    return w / w.sum()


# --- How busy is a given calendar day overall ------------------------------
def day_volume(date: pd.Timestamp, avg_daily: float, holidays: set,
               rng: np.random.Generator) -> int:
    """
    Expected toasts sold on this date. Weekends busier than weekdays;
    holidays busier still. Multiplicative noise for realism.
    Multipliers are tuned so the weekly mean stays near `avg_daily`.
    """
    dow = date.dayofweek  # 0=Mon .. 6=Sun
    if date.normalize() in holidays:
        mult = 1.85
    elif dow == 4:            # Friday
        mult = 1.15
    elif dow >= 5:            # Sat/Sun
        mult = 1.55
    else:                     # Mon-Thu
        mult = 0.80
    noise = rng.normal(1.0, 0.13)         # day-to-day swing
    vol = max(0.0, avg_daily * mult * noise)
    return int(round(vol))


def pick_holidays(dates: pd.DatetimeIndex, rng: np.random.Generator) -> set:
    """A scattering of ~12 busy 'holiday/event' days across the year."""
    idx = rng.choice(len(dates), size=min(12, len(dates)), replace=False)
    return {dates[i].normalize() for i in idx}


def simulate(cfg: Config, days: int, avg_daily: float, seed: int,
             start: str = "2025-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=days, freq="D")
    holidays = pick_holidays(dates, rng)
    weights = intraday_weights(cfg.open_minutes)

    rows = []
    order_id = 0
    for date in dates:
        target = day_volume(date, avg_daily, holidays, rng)
        if target <= 0:
            continue
        # Expected toasts per minute, then draw actual counts (Poisson).
        lam = weights * target
        counts = rng.poisson(lam)  # toasts starting each minute
        for minute, n in enumerate(counts):
            if n == 0:
                continue
            # Turn n toasts into 1..n orders (some customers buy 2-3).
            remaining = int(n)
            while remaining > 0:
                qty = min(remaining, 1 + rng.poisson(0.35))  # mostly 1
                qty = max(1, qty)
                remaining -= qty
                sec = int(rng.integers(0, 60))
                ts = (date
                      + pd.Timedelta(hours=cfg.open_hour)
                      + pd.Timedelta(minutes=int(minute), seconds=sec))
                order_id += 1
                rows.append((order_id, ts, ts.day_name(), cfg.item_name,
                             qty, cfg.price, qty * cfg.price))

    df = pd.DataFrame(rows, columns=[
        "order_id", "datetime", "day_of_week", "item",
        "quantity", "unit_price", "total"])
    df.sort_values("datetime", inplace=True, ignore_index=True)
    return df


def main():
    ap = argparse.ArgumentParser(
        description="Generate a synthetic year of POS data. "
                    "Shop inputs come from inputs.json (or --config), overridable by the flags below.")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--avg", type=float, default=250.0,
                    help="average toasts sold per day (across the year)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--out", default="data/pos_orders.csv")
    add_config_args(ap)
    args = ap.parse_args()

    try:
        cfg = resolve_config(args)
    except ValueError as err:
        ap.error(str(err))
    df = simulate(cfg, args.days, args.avg, args.seed, args.start)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    df.to_csv(args.out, index=False)

    total_toasts = int(df["quantity"].sum())
    n_days = df["datetime"].dt.normalize().nunique()
    print(f"Wrote {len(df):,} orders ({total_toasts:,} toasts) "
          f"over {n_days} days -> {args.out}")
    print(f"Average toasts/day: {total_toasts / n_days:,.1f}")
    by_dow = (df.assign(d=df['datetime'].dt.day_name())
                .groupby('d')['quantity'].sum()
                / df['datetime'].dt.normalize().nunique())
    print("Rough toasts/day by weekday name (sanity check):")
    print(by_dow.round(1).to_string())


if __name__ == "__main__":
    main()
