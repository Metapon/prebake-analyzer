"""
Business & operational parameters for the pre-bake analysis.

Everything the model needs to know about YOUR shop lives here. Change these
numbers (or pass a JSON file to analyze.py) and every report re-computes.
All money is in Thai Baht (THB). All times are in MINUTES unless noted.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import argparse
import json
import os

INPUTS_FILE = "inputs.json"   # default editable settings file


@dataclass
class Config:
    # ---- Money -----------------------------------------------------------
    price: float = 70.0          # selling price of one toast
    cog: float = 10.0            # cost of goods (ingredients) per toast
    # margin is derived: 60 THB. A wasted (stale) toast loses `cog`.

    # ---- Oven / production ----------------------------------------------
    bake_time_min: float = 6.0   # fastest a toast bakes
    bake_time_max: float = 8.0   # slowest
    oven_slots: int = 8          # how many toasts bake in PARALLEL
    order_service_min: float = 1.0   # time to take an order / hand over

    # ---- Freshness (the whole point) ------------------------------------
    # A baked toast is only sellable for this many minutes after it leaves
    # the oven. We NEVER sell past this. This doubles as the "prediction
    # tolerance" for a pre-baked toast: a customer must arrive inside it.
    fresh_window: float = 2.0

    # ---- Customer behaviour ---------------------------------------------
    # If a customer's expected total wait exceeds this, they walk out
    # (a "balk" = a lost 60 THB margin). This is how a queue actually
    # costs you money.
    patience_min: float = 10.0

    # ---- Opening hours ---------------------------------------------------
    open_hour: int = 11          # 11:00
    close_hour: int = 21         # 21:00 (9 PM)

    # ---- Pre-bake policy knobs ------------------------------------------
    # Only recommend pre-baking a unit if its chance of selling fresh is
    # at least this. Higher = more conservative = less waste, less coverage.
    success_threshold: float = 0.75

    # Only pre-bake into SPARE oven capacity (never steal a slot from a
    # customer who would have waited). This is the safe, correct default.
    # Set False to explore aggressive pre-baking that fights the oven.
    respect_oven_capacity: bool = True

    # ---- Data schema (so you can toss in a real POS export) -------------
    # Column names the analyzer looks for in your CSV. Rename to match
    # your POS export and nothing else has to change.
    col_datetime: str = "datetime"
    col_item: str = "item"
    col_qty: str = "quantity"
    item_name: str = "Toast"     # which product to analyze

    # ---------------------------------------------------------------------
    @property
    def margin(self) -> float:
        return self.price - self.cog

    @property
    def bake_time_nominal(self) -> float:
        return (self.bake_time_min + self.bake_time_max) / 2.0

    @property
    def open_minutes(self) -> int:
        """Total minutes the shop is open (e.g. 11:00-21:00 = 600)."""
        return (self.close_hour - self.open_hour) * 60

    def minute_to_clock(self, m: float) -> str:
        """Convert 'minutes since open' -> 'HH:MM'."""
        total = int(round(self.open_hour * 60 + m))
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"

    def validate(self) -> "Config":
        """Catch nonsensical inputs early with a clear message."""
        e = []
        if self.price <= 0:
            e.append("price must be > 0")
        if self.cog < 0:
            e.append("cog must be >= 0")
        if self.cog >= self.price:
            e.append(f"cog ({self.cog}) must be less than price ({self.price})")
        if self.bake_time_min <= 0 or self.bake_time_max < self.bake_time_min:
            e.append("need 0 < bake_time_min <= bake_time_max")
        if self.oven_slots < 1:
            e.append("oven_slots must be >= 1")
        if self.order_service_min < 0:
            e.append("order_service_min must be >= 0")
        if self.fresh_window <= 0:
            e.append("fresh_window must be > 0")
        if self.patience_min <= 0:
            e.append("patience_min must be > 0")
        if not (0 <= self.open_hour < self.close_hour <= 24):
            e.append("need 0 <= open_hour < close_hour <= 24")
        if not (0.0 < self.success_threshold < 1.0):
            e.append("success_threshold must be between 0 and 1 (e.g. 0.75)")
        if e:
            raise ValueError("Invalid inputs:\n  - " + "\n  - ".join(e))
        return self

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as f:
            data = json.load(f)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# ---------------------------------------------------------------------------
# Adjustable inputs: file (inputs.json) + command-line flags, one shared spec.
# ---------------------------------------------------------------------------
# (flag, config_field, python_type, help text)  -- every tunable lives here.
TUNABLE = [
    ("--price",             "price",              float, "selling price per toast (THB)"),
    ("--cog",               "cog",                float, "cost of goods per toast (THB)"),
    ("--bake-min",          "bake_time_min",      float, "fastest bake time (min)"),
    ("--bake-max",          "bake_time_max",      float, "slowest bake time (min)"),
    ("--oven-slots",        "oven_slots",         int,   "toasts the oven bakes in parallel"),
    ("--order-time",        "order_service_min",  float, "minutes to take an order / hand over"),
    ("--fresh-window",      "fresh_window",       float, "minutes a toast stays sellable (freshness)"),
    ("--patience",          "patience_min",       float, "minutes a customer waits before walking out"),
    ("--open-hour",         "open_hour",          int,   "opening hour, 24h (e.g. 11)"),
    ("--close-hour",        "close_hour",         int,   "closing hour, 24h (e.g. 21)"),
    ("--success-threshold", "success_threshold",  float, "min chance a pre-bake sells fresh to recommend it (0-1)"),
    ("--item",              "item_name",          str,   "product name to analyze in the POS data"),
    ("--col-datetime",      "col_datetime",       str,   "POS timestamp column name"),
    ("--col-item",          "col_item",           str,   "POS item/product column name"),
    ("--col-qty",           "col_qty",            str,   "POS quantity column name"),
]


def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Attach --config plus one flag per tunable. Defaults are None so we can
    tell which the user actually set (those override the file)."""
    g = parser.add_argument_group("shop inputs (override inputs.json)")
    g.add_argument("--config", default=None,
                   help=f"settings file to load (default: {INPUTS_FILE} if present)")
    for flag, field, typ, helptext in TUNABLE:
        g.add_argument(flag, dest=field, type=typ, default=None, help=helptext)
    g.add_argument("--respect-oven-capacity", dest="respect_oven_capacity",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="only pre-bake into spare oven capacity (safe default: on)")


def resolve_config(args: argparse.Namespace) -> Config:
    """Build a validated Config: start from the file, then apply CLI overrides.
    Priority (low -> high): defaults < inputs.json (or --config) < CLI flags."""
    path = getattr(args, "config", None) or INPUTS_FILE
    cfg = Config.load(path) if os.path.exists(path) else Config()
    for _flag, field, _typ, _h in TUNABLE:
        v = getattr(args, field, None)
        if v is not None:
            setattr(cfg, field, v)
    roc = getattr(args, "respect_oven_capacity", None)
    if roc is not None:
        cfg.respect_oven_capacity = roc
    return cfg.validate()


def write_template(path: str = INPUTS_FILE) -> None:
    """Write an editable settings file pre-filled with the current defaults."""
    Config().save(path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Write an editable inputs.json and show what each setting means.")
    add_config_args(ap)
    args = ap.parse_args()
    cfg = resolve_config(args)
    out = getattr(args, "config", None) or INPUTS_FILE
    cfg.save(out)
    print(f"Wrote {out} - edit any number below, then re-run analyze.py\n")
    for flag, field, _typ, helptext in TUNABLE:
        print(f"  {field:20} = {getattr(cfg, field)!s:<10} {helptext}")
    print(f"  {'respect_oven_capacity':20} = {cfg.respect_oven_capacity!s:<10} "
          "only pre-bake into spare oven capacity")
    print(f"\n  derived: margin {cfg.margin:.0f} THB | "
          f"nominal bake {cfg.bake_time_nominal:.1f} min | "
          f"open {cfg.open_minutes} min/day")
