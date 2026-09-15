"""Gamma exposure of the SPX option surface, from today's Cboe chain.

One free HTTP call returns the whole surface: every strike, open interest,
implied volatility, the exchange's own gamma, and the spot in the same payload.

What GEX measures
-----------------
A dealer who is long gamma must sell into rallies and buy into dips to stay
delta neutral, which damps the index. A dealer who is short gamma must do the
opposite, which amplifies it. Gamma exposure counts, per 1% move in SPX, how
many dollars of underlying the dealers as a group have to trade:

    GEX(contract) = gamma x open interest x multiplier x S x S x 0.01
                  = gamma x open interest x S^2          (multiplier 100)

The sign convention is the standard one and it is an ASSUMPTION, not data:
customers buy puts for protection so dealers are short puts, and customers
sell calls for yield so dealers are long calls. Calls therefore enter positive
and puts negative. Nobody outside the dealers knows the true book; every
retail GEX number rests on this, and it should be said out loud rather than
inherited.

The zero-gamma level is the spot at which total GEX crosses zero. Finding it
means re-pricing gamma at hypothetical spots, which Cboe's snapshot cannot do,
so gamma is recomputed from Black-Scholes across a grid. `--validate` checks
that reconstruction against Cboe's published gamma at today's spot before any
of it is trusted.

  python gex.py
  python gex.py --max-dte 90 --validate
  python gex.py --save            # write the chain and the profile to out/
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
CHAIN = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
UA = {"User-Agent": "gex-strategy/1.0 (research)"}
MULTIPLIER = 100
# OCC symbol: root, then yymmdd, then C or P, then strike in thousandths.
OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")


def fetch(symbol: str = "_SPX") -> tuple[pd.DataFrame, float, str]:
    """The whole surface, one call. Returns the chain, spot and the stamp."""
    raw = json.loads(urllib.request.urlopen(
        urllib.request.Request(CHAIN.format(symbol), headers=UA), timeout=90).read())
    d = raw["data"]
    spot = float(d["current_price"])
    rows = []
    for o in d["options"]:
        m = OCC.match(o["option"])
        if not m:
            continue
        rows.append({
            # SPX and SPXW share expiry/strike/side, so the root is part of a
            # contract's identity. Dropping it silently collides the two books.
            "root": m["root"],
            "expiry": date(2000 + int(m["ymd"][:2]), int(m["ymd"][2:4]), int(m["ymd"][4:])),
            "side": m["side"],
            "strike": int(m["strike"]) / 1000.0,
            "oi": float(o["open_interest"]),
            "gamma_cboe": float(o["gamma"]),
            "iv": float(o["iv"]),
            "volume": float(o["volume"]),
        })
    df = pd.DataFrame(rows)
    today = date.today()
    df["dte"] = [(e - today).days for e in df["expiry"]]
    return df, spot, raw.get("timestamp", datetime.now().isoformat(timespec="seconds"))


def bs_gamma(S: float, K, T, sigma, r: float = 0.04) -> np.ndarray:
    """Black-Scholes gamma. Used only to re-price the surface at other spots.

    T is floored at four hours so a contract expiring today does not divide by
    zero and produce an infinite gamma that swamps the total.
    """
    K = np.asarray(K, float)
    T = np.maximum(np.asarray(T, float), 4 / (365 * 24))
    sigma = np.maximum(np.asarray(sigma, float), 1e-4)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return np.exp(-0.5 * d1 ** 2) / (np.sqrt(2 * np.pi) * S * sigma * np.sqrt(T))


def gex_at(df: pd.DataFrame, S: float, gamma: np.ndarray) -> np.ndarray:
    """Dollar gamma exposure per contract, per 1% move, dealer sign applied."""
    sign = np.where(df["side"].to_numpy() == "C", 1.0, -1.0)
    return sign * gamma * df["oi"].to_numpy() * MULTIPLIER * S * S * 0.01


def flip_level(df: pd.DataFrame, spot: float, lo=0.85, hi=1.15, n=241):
    """Total GEX across a grid of hypothetical spots, and the zero crossing."""
    grid = np.linspace(spot * lo, spot * hi, n)
    total = np.array([gex_at(df, S, bs_gamma(S, df["strike"], df["dte"] / 365.0,
                                             df["iv"])).sum() for S in grid])
    flip = None
    for i in range(len(grid) - 1):
        a, b = total[i], total[i + 1]
        if a == 0 or (a < 0 < b) or (a > 0 > b):        # linear interpolation
            flip = grid[i] + (grid[i + 1] - grid[i]) * (-a) / (b - a) if b != a else grid[i]
            break
    return grid, total, flip


def money(x: float) -> str:
    for unit, div in (("bn", 1e9), ("mn", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"{x / div:+,.2f} {unit}"
    return f"{x:+,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="_SPX")
    ap.add_argument("--max-dte", type=int, default=365, help="ignore expiries beyond this")
    ap.add_argument("--validate", action="store_true",
                    help="check the Black-Scholes reconstruction against Cboe's gamma")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    df, spot, stamp = fetch(args.symbol)
    n_all = len(df)
    df = df[(df.dte >= 0) & (df.dte <= args.max_dte) & (df.oi > 0)].reset_index(drop=True)

    print("=" * 78)
    print(f"  SPX GAMMA EXPOSURE   {stamp}")
    print("=" * 78)
    print(f"  spot {spot:,.2f}   {n_all:,} contracts on the surface, "
          f"{len(df):,} with open interest inside {args.max_dte} days")
    print(f"  expiries {df.expiry.nunique()}   strikes {df.strike.nunique()}   "
          f"total open interest {df.oi.sum():,.0f}")

    if args.validate:
        mine = bs_gamma(spot, df["strike"], df["dte"] / 365.0, df["iv"])
        ok = df["gamma_cboe"] > 0
        a, b = mine[ok.to_numpy()], df.loc[ok, "gamma_cboe"].to_numpy()
        print(f"\n  reconstruction check, my Black-Scholes gamma vs Cboe's published gamma")
        print(f"    correlation {np.corrcoef(a, b)[0, 1]:.4f} across {ok.sum():,} contracts")
        print(f"    median ratio mine/theirs {np.median(a / b):.3f}")

    # today's exposure, on the exchange's own gamma: no assumptions but the sign
    df["gex"] = gex_at(df, spot, df["gamma_cboe"].to_numpy())
    calls = df.loc[df.side == "C", "gex"].sum()
    puts = df.loc[df.side == "P", "gex"].sum()
    total = calls + puts

    print(f"\n  ---- dealer gamma exposure, dollars per 1% move in SPX ----")
    print(f"    calls   {money(calls):>16}")
    print(f"    puts    {money(puts):>16}")
    print(f"    TOTAL   {money(total):>16}   -> dealers are "
          f"{'LONG gamma (moves damped)' if total > 0 else 'SHORT gamma (moves amplified)'}")

    grid, curve, flip = flip_level(df, spot)
    if flip:
        print(f"\n    zero-gamma level {flip:,.0f}   spot is "
              f"{(spot / flip - 1) * 100:+.2f}% {'above' if spot > flip else 'below'} it")
    else:
        print(f"\n    no zero crossing within {grid[0]:,.0f} to {grid[-1]:,.0f}")

    by_strike = (df.groupby("strike")["gex"].sum().sort_values(key=abs, ascending=False))
    print(f"\n  ---- ten strikes carrying the most gamma ----")
    print(f"    {'strike':>9}{'GEX':>16}{'vs spot':>10}   open interest")
    for k, v in by_strike.head(10).items():
        oi = df.loc[df.strike == k, "oi"].sum()
        print(f"    {k:>9,.0f}{money(v):>16}{(k / spot - 1) * 100:>9.1f}%   {oi:>12,.0f}")

    near = df[df.dte <= 7]
    print(f"\n  ---- concentration ----")
    print(f"    expiring within 7 days: {money(near.gex.sum())}  "
          f"({100 * abs(near.gex.sum()) / max(abs(total), 1):.0f}% of the total in absolute terms)")
    top5 = by_strike.head(5).sum()
    print(f"    top five strikes:       {money(top5)}  "
          f"({100 * abs(top5) / max(abs(total), 1):.0f}%)")

    if args.save:
        OUT.mkdir(exist_ok=True)
        day = date.today().isoformat()
        df.to_csv(OUT / f"{day}_spx_gex.csv", index=False)
        pd.DataFrame({"spot": grid, "total_gex": curve}).to_csv(
            OUT / f"{day}_spx_gex_profile.csv", index=False)
        print(f"\n  saved to {OUT}")

    print("\n  Sign convention assumed: dealers long calls, short puts. It is an")
    print("  assumption about a book nobody outside the dealers can see.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
