"""Map external feeds' scheme names (AMFI TER, fund performance, documents) to AMFI codes.

Feeds identify a scheme by its base name ("HDFC Flexi Cap Fund") while AMFI codes exist per
plan and option. A scheme's codes share one normalised base name, so the index maps
normalised name -> every AMFI code of that scheme. Exact match first; a strict fuzzy
fallback (same brand word, token-set ratio >= 95) handles spacing/punctuation drift.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Scheme

_PLAN_WORDS = re.compile(
    r"\b(regular|direct|plan|growth|option|idcw|dividend|payout|reinvest(ment)?|bonus|cumulative|"
    r"income distribution cum capital withdrawal|monthly|quarterly|half yearly|annual|weekly|daily|fortnightly)\b"
)


def norm_name(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\(erstwhile[^)]*\)|\(formerly[^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _PLAN_WORDS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Match:
    codes: set[int]
    method: str  # exact | fuzzy
    score: float


class SchemeNameIndex:
    """Normalised base name -> AMFI codes, built once per sync from the scheme master."""

    def __init__(self, db: Session):
        self.by_name: dict[str, set[int]] = defaultdict(set)
        for code, name in db.execute(select(Scheme.amfi_code, Scheme.name)):
            self.by_name[norm_name(name)].add(code)
        self._names = list(self.by_name)

    def match(self, name: str, min_score: float = 95.0) -> Match | None:
        key = norm_name(name)
        if key in self.by_name:
            return Match(set(self.by_name[key]), "exact", 100.0)
        if not key:
            return None
        brand = key.split()[0]
        candidates = [n for n in self._names if n.split()[:1] == [brand]]
        best = process.extractOne(key, candidates, scorer=fuzz.token_set_ratio) if candidates else None
        if best and best[1] >= min_score:
            return Match(set(self.by_name[best[0]]), "fuzzy", float(best[1]))
        return None
