"""Daily cap on extraction spend, enforced before every Claude call.

1. Before a call: reserve its worst-case cost (estimated input + full max_tokens, at the dearest rates
   it could be billed at). The check and the insert share one IMMEDIATE transaction, so two
   processes ingesting at once can't both squeeze under the cap.
2. After the call: replace the reservation with the real cost from the API's `usage`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from approval import pricing
from approval.db import Database, iso, utc_now
from approval.pricing import Usage


class DayCapReached(Exception):
    def __init__(self, cap: Decimal, committed: Decimal, needed: Decimal):
        super().__init__(
            f"daily extraction cap ${cap:.2f} reached (committed ${committed:.4f}, this call could cost ${needed:.4f})"
        )


@dataclass(frozen=True)
class Reservation:
    id: int
    model: str
    usd: Decimal


class SpendLedger:
    def __init__(self, db: Database, day_cap: Decimal, clock=utc_now):
        self.db, self.day_cap, self.clock = db, day_cap, clock

    def _day(self, now: dt.datetime) -> str:
        return now.astimezone(dt.UTC).date().isoformat()

    def committed(self, day: str | None = None) -> Decimal:
        day = day or self._day(self.clock())
        row = self.db.one("SELECT COALESCE(SUM(usd_micros), 0) AS m FROM extraction_costs WHERE day = ?", (day,))
        return pricing.from_micros(row["m"])

    def reserve(self, source_name: str, model: str, worst_usd: Decimal) -> Reservation:
        now = self.clock()
        day = self._day(now)
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usd_micros), 0) AS m FROM extraction_costs WHERE day = ?", (day,)
            ).fetchone()
            committed = pricing.from_micros(row["m"])
            if committed + worst_usd > self.day_cap:
                raise DayCapReached(self.day_cap, committed, worst_usd)
            cur = conn.execute(
                "INSERT INTO extraction_costs (day, at, source_name, model, status, usd_micros) "
                "VALUES (?, ?, ?, ?, 'reserved', ?)",
                (day, iso(now), source_name[:200], model, pricing.to_micros(worst_usd)),
            )
        return Reservation(cur.lastrowid, model, worst_usd)

    def settle(self, res: Reservation, served_model: str, usd: Decimal, usage: Usage) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE extraction_costs SET status = 'settled', model = ?, usd_micros = ?, input_tokens = ?, "
                "output_tokens = ?, cache_write_tokens = ?, cache_read_tokens = ? WHERE id = ?",
                (
                    served_model,
                    pricing.to_micros(usd),
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_write_tokens,
                    usage.cache_read_tokens,
                    res.id,
                ),
            )

    def report(self, days: int = 7) -> list[dict]:
        since = (self.clock().astimezone(dt.UTC).date() - dt.timedelta(days=days - 1)).isoformat()
        rows = self.db.all(
            "SELECT day, model, COUNT(*) AS calls, SUM(usd_micros) AS m, SUM(input_tokens) AS inp, "
            "SUM(output_tokens) AS outp, SUM(cache_read_tokens) AS cread FROM extraction_costs "
            "WHERE day >= ? GROUP BY day, model ORDER BY day DESC, model",
            (since,),
        )
        return [
            {
                "day": r["day"],
                "model": r["model"],
                "calls": r["calls"],
                "usd": pricing.from_micros(r["m"]),
                "input_tokens": r["inp"],
                "output_tokens": r["outp"],
                "cache_read_tokens": r["cread"],
            }
            for r in rows
        ]
