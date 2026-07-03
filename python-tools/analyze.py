"""
analyze.py  -  Turn POS order data into a pre-bake plan + ROI.

Works on the simulated data OR a real POS export. Point it at a CSV with a
datetime column and an item/quantity column (names configurable in config.py)
and it will:

  1. ESTIMATE DEMAND   - average toasts/minute for each day of week.
  2. BUILD A SCHEDULE  - for each day of week, WHEN to start pre-baking and
                         HOW MANY, so each pre-baked toast has a high chance
                         of selling inside its freshness window.
  3. MEASURE ROI       - a Monte-Carlo simulation of the oven + queue +
                         customer patience, comparing "make-to-order only"
                         vs "make-to-order + pre-bake", in Baht.

Usage:
    python analyze.py                                  # uses data/pos_orders.csv
    python analyze.py --data path/to/pos_export.csv
    python analyze.py --data ... --config myshop.json --sims 300
"""
from __future__ import annotations
import argparse
import json
import math
import os
import numpy as np
import pandas as pd

from config import Config, add_config_args, resolve_config

DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]
DT = 0.25  # simulation time step in minutes (15 seconds)


# ===========================================================================
# 1. DEMAND ESTIMATION
# ===========================================================================
def prepare_df(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Clean & filter a raw POS DataFrame: parse timestamps, coerce quantities,
    keep only the product being analyzed. Tolerant of messy real-world exports."""
    df = df.copy()
    if cfg.col_datetime not in df.columns:
        raise ValueError(f"Timestamp column '{cfg.col_datetime}' not found. "
                         f"Columns are: {', '.join(map(str, df.columns))}")
    df[cfg.col_datetime] = pd.to_datetime(df[cfg.col_datetime], errors="coerce")
    df = df[df[cfg.col_datetime].notna()]
    if cfg.col_item in df.columns and str(cfg.item_name).strip():
        target = str(cfg.item_name).strip().lower()
        df = df[df[cfg.col_item].astype(str).str.strip().str.lower() == target]
    if cfg.col_qty in df.columns:
        df[cfg.col_qty] = pd.to_numeric(df[cfg.col_qty], errors="coerce").fillna(1)
    else:
        df[cfg.col_qty] = 1
    return df


def load_pos(path: str, cfg: Config) -> pd.DataFrame:
    return prepare_df(pd.read_csv(path), cfg)


def estimate_demand(df: pd.DataFrame, cfg: Config) -> dict:
    """
    Returns {day_of_week: lam_per_min array of length open_minutes},
    where lam_per_min[m] = average toasts arriving in minute m of the day.
    """
    d = df.copy()
    dt = d[cfg.col_datetime]
    d["dow"] = dt.dt.day_name()
    d["min_of_day"] = (dt.dt.hour - cfg.open_hour) * 60 + dt.dt.minute
    d["date"] = dt.dt.normalize()
    d = d[(d["min_of_day"] >= 0) & (d["min_of_day"] < cfg.open_minutes)]

    profiles = {}
    for dow in DOW_ORDER:
        sub = d[d["dow"] == dow]
        n_days = max(1, sub["date"].nunique())
        lam = np.zeros(cfg.open_minutes)
        if len(sub):
            grouped = sub.groupby("min_of_day")[cfg.col_qty].sum()
            lam[grouped.index.values] = grouped.values / n_days
        # light smoothing so noise doesn't drive the schedule
        lam = _smooth(lam, 5)
        profiles[dow] = lam
    return profiles


def _smooth(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return x
    k = np.ones(w) / w
    return np.convolve(x, k, mode="same")


# ===========================================================================
# 2. PRE-BAKE SCHEDULE (the "when + how many" recommendation)
# ===========================================================================
def poisson_sf(k: int, mu: float) -> float:
    """P(N >= k) for N ~ Poisson(mu)."""
    if k <= 0:
        return 1.0
    # 1 - CDF(k-1)
    cdf = 0.0
    term = math.exp(-mu)
    cdf += term  # P(0)
    for i in range(1, k):
        term *= mu / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def expected_min(mu: float, q: int) -> float:
    """E[min(N, q)] for N ~ Poisson(mu) = sum_{j=1..q} P(N >= j)."""
    return sum(poisson_sf(j, mu) for j in range(1, q + 1))


def build_schedule(lam: np.ndarray, cfg: Config, cap: bool | None = None) -> list[dict]:
    """
    Walk the day in freshness-window-sized bins. For each bin decide how many
    toasts to have READY at the bin start, capping quantity so the marginal
    unit still clears the success threshold.

    cap=None -> use cfg.respect_oven_capacity. cap=False gives the raw
    "candidate windows" (where demand is dense enough to sell fresh, ignoring
    the oven). cap=True additionally limits to spare oven capacity.
    """
    if cap is None:
        cap = cfg.respect_oven_capacity
    w = cfg.fresh_window
    bake = cfg.bake_time_nominal
    # How many toasts the whole oven can physically produce in one window.
    # We only ever pre-bake into SPARE capacity: pre-baking while the oven is
    # already saturated just steals a slot from a customer who would have
    # waited anyway (guaranteed sale -> possible waste). Never do that.
    oven_output_per_window = cfg.oven_slots * (w / bake)
    rows = []
    ws = 0.0
    while ws < cfg.open_minutes:
        lo, hi = int(ws), min(cfg.open_minutes, int(ws + w))
        mu = float(lam[lo:hi].sum()) if hi > lo else 0.0  # expected arrivals
        spare = max(0.0, oven_output_per_window - mu)      # free oven capacity
        # grow q while the q-th unit still sells fresh often enough
        q = 0
        while poisson_sf(q + 1, mu) >= cfg.success_threshold:
            q += 1
        if cap:
            q = min(q, int(math.floor(spare)))             # cap at spare capacity
        if q > 0:
            exp_sold = expected_min(mu, q)
            rows.append({
                "ready_min": ws,
                "start_min": ws - bake,
                "ready_clock": cfg.minute_to_clock(ws),
                "start_clock": cfg.minute_to_clock(ws - bake),
                "qty": q,
                "arrivals_expected": round(mu, 2),
                "p_first_sells": round(poisson_sf(1, mu), 3),
                "p_marginal_sells": round(poisson_sf(q, mu), 3),
                "exp_sold_fresh": round(exp_sold, 2),
                "exp_waste": round(q - exp_sold, 2),
            })
        ws += w
    return rows


def schedule_to_starts(schedule: list[dict], cfg: Config) -> dict:
    """Map schedule -> {step_index: number of prebakes to START at that step}."""
    starts = {}
    n_steps = int(cfg.open_minutes / DT)
    for r in schedule:
        s = r["start_min"]
        if s < 0:
            s = 0.0  # can't start before opening; best effort at open
        step = int(round(s / DT))
        if 0 <= step < n_steps:
            starts[step] = starts.get(step, 0) + r["qty"]
    return starts


# ===========================================================================
# 3. MONTE-CARLO EVALUATION (oven + queue + freshness + patience)
# ===========================================================================
def draw_arrivals(lam: np.ndarray, cfg: Config, rng: np.random.Generator) -> np.ndarray:
    """Number of customers arriving in each simulation step (one realised day)."""
    n_steps = int(cfg.open_minutes / DT)
    minutes = np.minimum((np.arange(n_steps) * DT).astype(int), cfg.open_minutes - 1)
    return rng.poisson(lam[minutes] * DT)


def simulate_day(lam: np.ndarray, cfg: Config, starts: dict,
                 rng: np.random.Generator, arrivals: np.ndarray | None = None) -> dict:
    n_steps = int(cfg.open_minutes / DT)
    if arrivals is None:
        arrivals = draw_arrivals(lam, cfg, rng)
    slot_free = np.zeros(cfg.oven_slots)     # time each oven slot is next free
    slot_prebake = np.zeros(cfg.oven_slots, dtype=bool)
    inventory: list[float] = []              # expiry times of ready prebakes

    served = prebaked_sold = balked = waste = 0

    for step in range(n_steps):
        now = step * DT

        # (a) completions: prebakes finishing go to fresh inventory
        for i in range(cfg.oven_slots):
            if slot_free[i] <= now and slot_prebake[i]:
                inventory.append(slot_free[i] + cfg.fresh_window)
                slot_prebake[i] = False

        # (b) expire stale prebakes (never sold in time) -> waste
        if inventory:
            keep = []
            for exp in inventory:
                if exp <= now:
                    waste += 1
                else:
                    keep.append(exp)
            inventory = keep

        # (c) start scheduled prebakes if an oven slot is free
        want = starts.get(step, 0)
        if want:
            free_idx = np.where(slot_free <= now)[0]
            for i in free_idx[:want]:
                slot_free[i] = now + rng.uniform(cfg.bake_time_min, cfg.bake_time_max)
                slot_prebake[i] = True

        # (d) arrivals in this step (shared across policies via `arrivals`)
        n_arr = arrivals[step]
        for _ in range(n_arr):
            if inventory:                       # a fresh toast is waiting
                inventory.pop(inventory.index(min(inventory)))
                served += 1
                prebaked_sold += 1
                continue
            # make-to-order
            free_idx = np.where(slot_free <= now)[0]
            if len(free_idx):
                wait = cfg.bake_time_nominal + cfg.order_service_min
                i = free_idx[0]
                slot_free[i] = now + rng.uniform(cfg.bake_time_min, cfg.bake_time_max)
                slot_prebake[i] = False
                served += 1
            else:
                i = int(np.argmin(slot_free))
                wait = (slot_free[i] - now) + cfg.bake_time_nominal + cfg.order_service_min
                if wait > cfg.patience_min:
                    balked += 1
                else:
                    slot_free[i] = slot_free[i] + rng.uniform(cfg.bake_time_min, cfg.bake_time_max)
                    served += 1

    baked = served + waste
    revenue = served * cfg.price
    profit = revenue - baked * cfg.cog
    return {
        "served": served, "prebaked_sold": prebaked_sold,
        "balked": balked, "waste": waste,
        "revenue": revenue, "profit": profit,
        "lost_margin": balked * cfg.margin,
    }


def evaluate_dow(lam: np.ndarray, cfg: Config, schedule: list[dict],
                 n_sims: int, rng: np.random.Generator) -> dict:
    starts = schedule_to_starts(schedule, cfg)
    base = {k: 0.0 for k in ["served", "balked", "waste", "profit",
                             "revenue", "prebaked_sold", "lost_margin"]}
    pol = {k: 0.0 for k in base}
    for _ in range(n_sims):
        arrivals = draw_arrivals(lam, cfg, rng)   # SAME customers for both
        b = simulate_day(lam, cfg, {}, rng, arrivals)      # make-to-order only
        p = simulate_day(lam, cfg, starts, rng, arrivals)  # + pre-bake
        for k in base:
            base[k] += b[k]
            pol[k] += p[k]
    for k in base:
        base[k] /= n_sims
        pol[k] /= n_sims

    waste_cost = pol["waste"] * cfg.cog
    gain = pol["profit"] - base["profit"]
    total_prebaked = pol["prebaked_sold"] + pol["waste"]
    success = (pol["prebaked_sold"] / total_prebaked) if total_prebaked else 0.0
    roi = (gain / waste_cost) if waste_cost > 1e-9 else float("inf")
    return {
        "baseline": base, "policy": pol,
        "daily_gain": gain, "waste_cost": waste_cost,
        "prebake_success_rate": success, "roi": roi,
        "prebakes_per_day": total_prebaked,
    }


# ===========================================================================
# ORCHESTRATION
# ===========================================================================
def dow_counts(df: pd.DataFrame, cfg: Config) -> dict:
    dates = df[cfg.col_datetime].dt.normalize().drop_duplicates()
    names = dates.dt.day_name()
    return names.value_counts().to_dict()


def _demand_buckets(lam: np.ndarray, cfg: Config, width: int = 10) -> list:
    """Down-sample a per-minute demand curve into width-minute points (for charts)."""
    out = []
    for m in range(0, cfg.open_minutes, width):
        seg = lam[m:m + width]
        out.append({"clock": cfg.minute_to_clock(m),
                    "rate": round(float(seg.mean()) if len(seg) else 0.0, 3)})
    return out


def _verdict(annual: dict, cfg: Config) -> dict:
    """Plain-English recommendation from the annual numbers."""
    gain = annual["gain"]
    wcost = annual["waste_cost"]
    recovered = annual["recovered_sales"]
    prebaked = annual["prebaked_units"]
    if prebaked < 1:
        return {
            "recommend": False,
            "headline": "Pre-baking won't help at these settings.",
            "detail": (f"Whenever demand is dense enough that a toast reliably sells "
                       f"within the {cfg.fresh_window:.0f}-min freshness window, your "
                       f"oven ({cfg.oven_slots} slots) is already at capacity — so there "
                       f"is no spare slot to pre-bake into. Your rush queue is a capacity "
                       f"limit, not a scheduling problem; adding oven capacity is the real fix."),
        }
    if gain > 0 and gain > max(500.0, 0.5 * wcost):
        return {
            "recommend": True,
            "headline": f"Pre-baking looks worth it: about +{gain:,.0f} THB/year.",
            "detail": (f"You would waste ~{annual['waste_units']:,.0f} toasts/year "
                       f"(~{wcost:,.0f} THB) but recover ~{recovered:,.0f} walk-out "
                       f"sales worth more than that. Follow the schedule below during "
                       f"the marked rush windows."),
        }
    return {
        "recommend": False,
        "headline": "Pre-baking roughly breaks even — not worth the effort here.",
        "detail": (f"It would cost ~{wcost:,.0f} THB/year in wasted toasts to recover "
                   f"only ~{recovered:,.0f} sales. The freshness window is too short to "
                   f"build a buffer, so there's little upside at this volume."),
    }


def run_analysis(df: pd.DataFrame, cfg: Config, sims: int = 150, seed: int = 1) -> dict:
    """
    Full pipeline on an already-loaded, item-filtered DataFrame.
    Returns a JSON-serializable dict with the schedule, per-weekday ROI,
    an annual summary, a plain-English verdict, and chart data.
    Shared by the CLI (main) and the web app.
    """
    rng = np.random.default_rng(seed)
    profiles = estimate_demand(df, cfg)
    counts = dow_counts(df, cfg)

    by_dow, schedule, demand_chart = [], [], {}
    annual = {"gain": 0.0, "waste_cost": 0.0, "recovered_sales": 0.0,
              "baseline_profit": 0.0, "policy_profit": 0.0,
              "baseline_balk": 0.0, "policy_balk": 0.0,
              "prebaked_units": 0.0, "waste_units": 0.0}

    for dow in DOW_ORDER:
        lam = profiles[dow]
        demand_chart[dow] = _demand_buckets(lam, cfg)
        candidate = build_schedule(lam, cfg, cap=False)
        for r in candidate:
            schedule.append({"day_of_week": dow, **r})
        policy_sched = build_schedule(lam, cfg, cap=True)
        ev = evaluate_dow(lam, cfg, policy_sched, sims, rng)
        n = counts.get(dow, 0)
        recovered = ev["baseline"]["balked"] - ev["policy"]["balked"]
        cand_units = sum(r["qty"] for r in candidate)
        cand_p = float(np.mean([r["p_first_sells"] for r in candidate])) if candidate else 0.0
        by_dow.append({
            "day_of_week": dow, "days_in_data": int(n),
            "avg_toasts_sold_day": round(ev["baseline"]["served"], 1),
            "candidate_units": int(cand_units),
            "candidate_avg_success": round(cand_p, 3),
            "prebake_units_day": round(ev["prebakes_per_day"], 1),
            "prebake_success_rate": round(ev["prebake_success_rate"], 3),
            "waste_units_day": round(ev["policy"]["waste"], 2),
            "balks_no_prebake": round(ev["baseline"]["balked"], 1),
            "balks_with_prebake": round(ev["policy"]["balked"], 1),
            "sales_recovered_day": round(recovered, 1),
            "daily_profit_gain_thb": round(ev["daily_gain"], 1),
            "roi_x": round(ev["roi"], 1) if math.isfinite(ev["roi"]) else None,
        })
        annual["gain"] += ev["daily_gain"] * n
        annual["waste_cost"] += ev["waste_cost"] * n
        annual["recovered_sales"] += recovered * n
        annual["baseline_profit"] += ev["baseline"]["profit"] * n
        annual["policy_profit"] += ev["policy"]["profit"] * n
        annual["baseline_balk"] += ev["baseline"]["balked"] * n
        annual["policy_balk"] += ev["policy"]["balked"] * n
        annual["prebaked_units"] += ev["prebakes_per_day"] * n
        annual["waste_units"] += ev["policy"]["waste"] * n

    total_toasts = int(df[cfg.col_qty].sum())
    n_days = int(df[cfg.col_datetime].dt.normalize().nunique())
    summary = {
        "annual_extra_profit_thb": round(annual["gain"]),
        "annual_waste_cost_thb": round(annual["waste_cost"]),
        "annual_sales_recovered": round(annual["recovered_sales"]),
        "annual_roi_multiple": (round(annual["gain"] / annual["waste_cost"], 1)
                                if annual["waste_cost"] > 1e-9 else None),
        "baseline_annual_profit_thb": round(annual["baseline_profit"]),
        "policy_annual_profit_thb": round(annual["policy_profit"]),
        "baseline_annual_walkouts": round(annual["baseline_balk"]),
        "policy_annual_walkouts": round(annual["policy_balk"]),
        "total_toasts": total_toasts, "n_days": n_days,
        "avg_per_day": round(total_toasts / n_days, 1) if n_days else 0,
    }
    return {
        "meta": {
            "item": cfg.item_name, "rows": int(len(df)),
            "days": n_days, "avg_per_day": summary["avg_per_day"],
            "capacity_per_min": round(cfg.oven_slots / cfg.bake_time_nominal, 3),
            "freshness_floor_per_min": round(
                -math.log(1 - cfg.success_threshold) / cfg.fresh_window, 3),
            "open_hour": cfg.open_hour, "close_hour": cfg.close_hour,
            "sims": sims,
        },
        "summary": summary,
        "verdict": _verdict(annual, cfg),
        "by_dow": by_dow,
        "schedule": schedule,
        "demand_chart": demand_chart,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Analyze POS data -> pre-bake schedule + ROI. "
                    "Inputs come from inputs.json (or --config), overridable by the flags below.")
    ap.add_argument("--data", default="data/pos_orders.csv")
    ap.add_argument("--sims", type=int, default=200,
                    help="Monte-Carlo runs per day-of-week")
    ap.add_argument("--outdir", default="reports")
    ap.add_argument("--seed", type=int, default=1)
    add_config_args(ap)
    args = ap.parse_args()

    try:
        cfg = resolve_config(args)
    except ValueError as err:
        ap.error(str(err))
    os.makedirs(args.outdir, exist_ok=True)
    df = load_pos(args.data, cfg)

    print("=" * 70)
    print("PRE-BAKE ANALYSIS")
    print(f"  price {cfg.price:.0f}  cog {cfg.cog:.0f}  margin {cfg.margin:.0f} THB | "
          f"bake {cfg.bake_time_min:.0f}-{cfg.bake_time_max:.0f}m | "
          f"fresh {cfg.fresh_window:.0f}m | oven {cfg.oven_slots} slots | "
          f"patience {cfg.patience_min:.0f}m")
    print("=" * 70)

    res = run_analysis(df, cfg, sims=args.sims, seed=args.seed)

    for row in res["by_dow"]:
        verdict = (f"ROI {row['roi_x']}x" if row["roi_x"] is not None
                   else "no safe pre-bake (no spare oven capacity)")
        print(f"{row['day_of_week']:<9} sold~{row['avg_toasts_sold_day']:5.0f}/day | "
              f"candidate windows {row['candidate_units']:3d}u "
              f"@~{row['candidate_avg_success']*100:2.0f}% fresh | "
              f"safe-policy prebake {row['prebake_units_day']:4.1f}u | "
              f"walk-out gain {row['daily_profit_gain_thb']:+6.0f} THB/day | {verdict}")

    # ---- write machine-readable reports ----
    profiles = estimate_demand(df, cfg)
    demand_rows = [{"day_of_week": dow, "minute": m, "clock": cfg.minute_to_clock(m),
                    "toasts_per_min": round(float(profiles[dow][m]), 4)}
                   for dow in DOW_ORDER for m in range(cfg.open_minutes)]
    pd.DataFrame(demand_rows).to_csv(f"{args.outdir}/demand_profile.csv", index=False)
    pd.DataFrame(res["schedule"]).to_csv(f"{args.outdir}/prebake_schedule.csv", index=False)
    pd.DataFrame(res["by_dow"]).to_csv(f"{args.outdir}/roi_by_dayofweek.csv", index=False)
    with open(f"{args.outdir}/roi_summary.json", "w") as f:
        json.dump(res["summary"], f, indent=2)

    s = res["summary"]
    print("=" * 70)
    print("ANNUAL SUMMARY (scaled by how often each weekday appears in data)")
    print(f"  Walk-outs  : {s['baseline_annual_walkouts']:,} -> "
          f"{s['policy_annual_walkouts']:,} lost sales/year")
    print(f"  Recovered  : {s['annual_sales_recovered']:,} sales/year")
    print(f"  Waste cost : {s['annual_waste_cost_thb']:,} THB/year")
    print(f"  EXTRA PROFIT: {s['annual_extra_profit_thb']:,} THB/year "
          f"(ROI {s['annual_roi_multiple']}x on waste spent)")
    print(f"\n  VERDICT: {res['verdict']['headline']}")
    print(f"\nReports written to {args.outdir}/")


if __name__ == "__main__":
    main()
