"""
sweep.py - Where does pre-baking actually pay?

Clean (common-random-number) ROI across a grid of oven sizes x patience.
Prints annual extra profit and decomposes it into recovered sales vs waste,
for both the SAFE policy (pre-bake only into spare capacity) and the
AGGRESSIVE policy (pre-bake on demand density alone).
"""
import numpy as np
from config import Config
from analyze import (load_pos, estimate_demand, build_schedule,
                     evaluate_dow, dow_counts, DOW_ORDER)

DATA = "data/pos_orders.csv"

def annual(cfg, profiles, counts, sims):
    rng = np.random.default_rng(1)
    g = wc = recov = ps = pw = 0.0
    for dow in DOW_ORDER:
        lam = profiles[dow]
        ev = evaluate_dow(lam, cfg, build_schedule(lam, cfg), sims, rng)
        n = counts.get(dow, 0)
        g += ev["daily_gain"] * n
        wc += ev["waste_cost"] * n
        recov += (ev["baseline"]["balked"] - ev["policy"]["balked"]) * n
        ps += ev["policy"]["prebaked_sold"] * n
        pw += ev["policy"]["waste"] * n
    succ = ps / (ps + pw) if (ps + pw) else 0
    return dict(gain=g, waste_cost=wc, recovered=recov, prebaked=ps + pw, success=succ)

def grid(profiles, counts, sims, respect):
    ovens = [3, 4, 5, 6, 8]
    pats = [8, 10, 12]
    tag = "SAFE (spare-capacity only)" if respect else "AGGRESSIVE (density only)"
    print(f"\n=== {tag} : annual extra profit THB (recovered sales | waste units) ===")
    print(f"{'':6}" + "".join(f"{'patience='+str(p):>26}" for p in pats))
    for slots in ovens:
        cells = []
        for p in pats:
            cfg = Config(oven_slots=slots, patience_min=p, respect_oven_capacity=respect)
            r = annual(cfg, profiles, counts, sims)
            cells.append(f"{r['gain']:+7.0f} ({r['recovered']:+5.0f} | {r['prebaked']-r['recovered']*0:5.0f}w)")
        print(f"oven{slots:<2}" + "".join(f"{c:>26}" for c in cells))

def main():
    base = Config()
    df = load_pos(DATA, base)
    profiles = estimate_demand(df, base)
    counts = dow_counts(df, base)
    grid(profiles, counts, 200, respect=True)
    grid(profiles, counts, 200, respect=False)

if __name__ == "__main__":
    main()
