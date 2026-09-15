"""The one decision the strategy makes: at 15:30 ET, which way to hold the last
half hour, in Micro E-mini S&P 500 futures, flat on the close.

The rule
--------
  regime   sign of SqueezeMetrics net gamma at the previous close (regime.py)
  day      SPX return from the 09:30 open to 15:30
  short gamma  ->  go WITH the day      (Baltussen, Da, Lammers, Martens, JFE 2021:
                                         slope 6.63, t 4.78; Firmtape 2022-2026: t 3.1;
                                         our SPY 2023-2026: t 2.8, 62% hit)
  long gamma   ->  go AGAINST the day   (our SPY 2023-2026: slope -0.035, t -2.8;
                                         Barbon and Buraschi 2021 report reversal
                                         under positive gamma; Baltussen found no
                                         effect. The weaker leg. --no-fade turns it off.)
  exit         market on close, every day, no overnight

Sizing is one number: notional as a multiple of equity. At 1.0 and $1m that
is about 26 MES. The traded stream ran near 0.2% of notional per day of
standard deviation in the tests, so 1.0 is a quiet setting.

No broker here. This prints the decision; the engine will place it.

  python last_hour.py                      # live: regime + SPX day so far, from free data
  python last_hour.py --equity 1000000 --leverage 1.0 --no-fade
  python last_hour.py --gex=-1 --sofar 0.004  # offline: hand it the two inputs
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

import numpy as np

MES_MULT = 5.0


@dataclass
class Decision:
    action: str          # LONG, SHORT or FLAT
    contracts: int
    why: str
    leg: str             # follow, fade or none


def decide(gex_prev: float, r_sofar: float, equity: float, spot: float,
           leverage: float = 1.0, fade_long_gamma: bool = True) -> Decision:
    side = int(np.sign(r_sofar))
    n = int(equity * leverage // (MES_MULT * spot))
    if side == 0 or n == 0:
        return Decision("FLAT", 0, "no move so far, or no size", "none")
    if gex_prev < 0:
        return Decision("LONG" if side > 0 else "SHORT", n,
                        "short gamma: dealers hedge with the move into the close, so go with the day", "follow")
    if fade_long_gamma:
        return Decision("SHORT" if side > 0 else "LONG", n,
                        "long gamma: dealers lean against the move into the close, so go against the day", "fade")
    return Decision("FLAT", 0, "long gamma and the fade leg is off", "none")


def spx_day_so_far() -> tuple[float, float, float, str]:
    """Open and latest SPX from Yahoo one-minute bars. The engine uses IB instead."""
    import yfinance as yf
    h = yf.Ticker("^GSPC").history(period="1d", interval="1m")
    if h.empty:
        raise SystemExit("no intraday SPX bars, is the market open?")
    o, last = float(h.Open.iloc[0]), float(h.Close.iloc[-1])
    return o, last, np.log(last / o), str(h.index[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=1_000_000)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--no-fade", action="store_true", help="trade the short gamma leg only")
    ap.add_argument("--gex", type=float, default=None, help="offline: previous close net gamma, sign is what matters")
    ap.add_argument("--sofar", type=float, default=None, help="offline: SPX log return open to now")
    ap.add_argument("--spot", type=float, default=None)
    a = ap.parse_args()

    if a.gex is None:
        from regime import regime_for
        r = regime_for(date.today())
        gex, asof = r["gex"], r["asof"]
    else:
        gex, asof = a.gex, "given"
    if a.sofar is None:
        o, last, sofar, stamp = spx_day_so_far()
        spot = last
    else:
        sofar, spot, stamp = a.sofar, a.spot or 7650.0, "given"
        o, last = spot / np.exp(sofar), spot

    d = decide(gex, sofar, a.equity, spot, a.leverage, not a.no_fade)
    print("=" * 72)
    print(f"  15:30 DECISION    regime from {asof}   net gamma {gex / 1e9:+.2f} bn   "
          f"{'SHORT' if gex < 0 else 'LONG'} GAMMA")
    print("=" * 72)
    print(f"  SPX open {o:,.2f}   now {last:,.2f}   day so far {100 * sofar:+.2f}%   ({stamp})")
    print(f"  action   {d.action} {d.contracts} MES   notional {d.contracts * MES_MULT * spot:,.0f}")
    print(f"  why      {d.why}")
    print(f"  exit     market on close, no overnight")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
