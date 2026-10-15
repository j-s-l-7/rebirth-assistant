"""Read-through access to NAV history with a per-request cache."""

from bisect import bisect_left, bisect_right
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NavPoint, Scheme


class NavBook:
    def __init__(self, db: Session):
        self.db = db
        self._series: dict[int, tuple[list[date], list[float]]] = {}

    def series(self, code: int, since: date | None = None) -> tuple[list[date], list[float]]:
        if code not in self._series:
            rows = self.db.execute(
                select(NavPoint.nav_date, NavPoint.nav).where(NavPoint.amfi_code == code).order_by(NavPoint.nav_date)
            ).all()
            dates, navs = [r[0] for r in rows], [float(r[1]) for r in rows]
            if not dates:
                s = self.db.get(Scheme, code)
                if s and s.latest_nav and s.latest_nav_date:
                    dates, navs = [s.latest_nav_date], [float(s.latest_nav)]
            self._series[code] = (dates, navs)
        dates, navs = self._series[code]
        if since:
            i = bisect_left(dates, since)
            return dates[i:], navs[i:]
        return dates, navs

    def preload(self, codes: set[int]) -> None:
        missing = [c for c in codes if c not in self._series]
        if not missing:
            return
        rows = self.db.execute(
            select(NavPoint.amfi_code, NavPoint.nav_date, NavPoint.nav)
            .where(NavPoint.amfi_code.in_(missing))
            .order_by(NavPoint.amfi_code, NavPoint.nav_date)
        ).all()
        grouped: dict[int, tuple[list[date], list[float]]] = {c: ([], []) for c in missing}
        for code, d, n in rows:
            grouped[code][0].append(d)
            grouped[code][1].append(float(n))
        for code, (dates, navs) in grouped.items():
            if dates:
                self._series[code] = (dates, navs)
            else:
                self.series(code)  # falls back to the scheme's latest NAV

    def nav_on(self, code: int, d: date) -> float | None:
        """Last available NAV on or before `d`."""
        dates, navs = self.series(code)
        i = bisect_right(dates, d)
        return navs[i - 1] if i else None

    def nav_on_or_after(self, code: int, d: date) -> tuple[date, float] | None:
        dates, navs = self.series(code)
        i = bisect_left(dates, d)
        return (dates[i], navs[i]) if i < len(dates) else None

    def latest(self, code: int) -> tuple[date, float] | None:
        dates, navs = self.series(code)
        return (dates[-1], navs[-1]) if dates else None

    def previous(self, code: int) -> tuple[date, float] | None:
        dates, navs = self.series(code)
        return (dates[-2], navs[-2]) if len(dates) >= 2 else None

    def first_date(self, code: int) -> date | None:
        dates, _ = self.series(code)
        return dates[0] if dates else None

    def month_end_series(self, code: int, since: date | None = None) -> list[tuple[date, float]]:
        dates, navs = self.series(code, since)
        out: list[tuple[date, float]] = []
        for d, n in zip(dates, navs):
            if out and (out[-1][0].year, out[-1][0].month) == (d.year, d.month):
                out[-1] = (d, n)
            else:
                out.append((d, n))
        return out
