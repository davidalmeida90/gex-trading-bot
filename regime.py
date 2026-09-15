"""Which gamma regime the day opens in.

The evidence for the strategy (Baltussen et al. 2021, the Firmtape 2022-2026
replication, and the backtests summarised in the README) was all
built on one series: SqueezeMetrics' daily SPX net gamma, dealers long calls
and short puts, published free at

    https://squeezemetrics.com/monitor/static/DIX.csv

So that series is the regime input, not our own Cboe computation. The two do
not always agree. On 9 September 2026 SqueezeMetrics read +5.1 bn while the
full Cboe book, weeklies and same-day contracts included, read -36 bn. The
disagreement sits in the short-dated put book, and the rule was never tested
on that definition, so it is not used to trade. It is printed as a cross-check.

Timing: the row for date t is the close of day t. A decision at 15:30 on day t
uses the row for the last completed session before t, which is what the tests
conditioned on (neg_prev). Nothing looks ahead.

  python regime.py            # today's regime, with the Cboe cross-check
  python regime.py --no-cboe  # the regime alone, fast
"""

from __future__ import annotations

import argparse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CSV = HERE / "data" / "squeezemetrics_spx_dix_gex.csv"
URL = "https://squeezemetrics.com/monitor/static/DIX.csv"
UA = {"User-Agent": "Mozilla/5.0"}


def load(max_age_hours: float = 6.0) -> pd.DataFrame:
    """The full history, re-downloaded when the local copy is stale."""
    fresh = CSV.exists() and (datetime.now() - datetime.fromtimestamp(CSV.stat().st_mtime)
                              < timedelta(hours=max_age_hours))
    if not fresh:
        raw = urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=60).read()
        if not raw.startswith(b"date,"):
            if CSV.exists():
                print("  warning: download did not look like the CSV, using the local copy")
            else:
                raise SystemExit("SqueezeMetrics CSV unavailable and no local copy")
        else:
            CSV.parent.mkdir(exist_ok=True)
            CSV.write_bytes(raw)
    d = pd.read_csv(CSV, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    return d


def regime_for(session: date, d: pd.DataFrame | None = None) -> dict:
    """The regime that applies to a decision taken during `session`."""
    d = load() if d is None else d
    prior = d[d.date.dt.date < session]
    if prior.empty:
        raise SystemExit("no SqueezeMetrics row before the session")
    row = prior.iloc[-1]
    stale = (session - row.date.date()).days
    return dict(asof=row.date.date(), gex=float(row.gex), spx=float(row.price),
                short_gamma=bool(row.gex < 0), stale_days=stale,
                share_short_1y=float((d.tail(252).gex < 0).mean()))


def cboe_crosscheck() -> dict:
    """Our own number from the live Cboe chain, for the record only."""
    import gex as G
    df, spot, stamp = G.fetch("_SPX")
    df = df[(df.dte >= 0) & (df.oi > 0)].reset_index(drop=True)
    x = G.gex_at(df, spot, df["gamma_cboe"].to_numpy())
    full = float(x.sum())
    beyond_week = float(x[(df.dte > 7).to_numpy()].sum())
    return dict(stamp=stamp, spot=spot, full_book_bn=full / 1e9, beyond_7d_bn=beyond_week / 1e9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cboe", action="store_true")
    ap.add_argument("--date", default=None, help="session date, default today")
    a = ap.parse_args()
    session = date.fromisoformat(a.date) if a.date else date.today()
    r = regime_for(session)
    print("=" * 72)
    stale = "" if r["stale_days"] <= 3 else f"   ({r['stale_days']} days old, check the feed)"
    print(f"  REGIME FOR {session}   from the SqueezeMetrics close of {r['asof']}{stale}")
    print("=" * 72)
    print(f"  net gamma   {r['gex'] / 1e9:+.2f} bn   SPX {r['spx']:,.0f}")
    print(f"  regime      {'SHORT GAMMA, dealers chase, follow the day into the close' if r['short_gamma'] else 'LONG GAMMA, dealers lean against, fade the day into the close'}")
    print(f"  base rate   short gamma on {100 * r['share_short_1y']:.0f}% of the last 252 sessions")
    if not a.no_cboe:
        try:
            c = cboe_crosscheck()
            print(f"\n  Cboe cross-check {c['stamp']}   spot {c['spot']:,.2f}")
            print(f"    full book, all expiries   {c['full_book_bn']:+.1f} bn per 1%")
            print(f"    beyond seven days         {c['beyond_7d_bn']:+.1f} bn per 1%")
            agree = (c['full_book_bn'] < 0) == r['short_gamma']
            print(f"    sign {'agrees' if agree else 'DISAGREES'} with SqueezeMetrics. "
                  f"The rule was tested on their series, so their sign is the one used.")
        except Exception as e:                                    # noqa: BLE001
            print(f"\n  Cboe cross-check skipped: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
