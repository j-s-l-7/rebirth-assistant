"""Original AMC disclosure files: copy into backend/data/source_files/ (git-ignored) and register by sha256."""

import calendar
import hashlib
import re
import shutil
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BACKEND_DIR
from app.models import SourceFile

SOURCE_DIR = BACKEND_DIR / "data" / "source_files"
EXTS = {".xls", ".xlsx", ".zip"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def resolve_stored(stored_path: str) -> Path | None:
    """Absolute path of a registered file, or None if it would fall outside SOURCE_DIR or is missing."""
    root = SOURCE_DIR.resolve()
    p = (root / stored_path).resolve()
    return p if p.is_relative_to(root) and p.is_file() else None


def register_files(db: Session, paths: list[str], amc: str, month: str, source_url: str, via: str) -> dict:
    year, mon = (int(x) for x in month.split("-"))
    disclosed = date(year, mon, calendar.monthrange(year, mon)[1])
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        files += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in EXTS) if p.is_dir() else [p]
    added = skipped = size = 0
    folder = Path(slug(amc)) / month
    for f in files:
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if db.scalar(select(SourceFile.id).where(SourceFile.sha256 == sha)):
            skipped += 1
            continue
        dest = SOURCE_DIR / folder / f.name
        if dest.exists():
            dest = dest.with_name(f"{sha[:8]}-{f.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)
        db.add(SourceFile(amc_name=amc, disclosure_month=disclosed, filename=f.name, sha256=sha, size_bytes=len(data),
                          source_url=source_url, via=via, stored_path=str(dest.relative_to(SOURCE_DIR))))
        added += 1
        size += len(data)
    db.commit()
    return {"added": added, "skipped_duplicates": skipped, "mb": round(size / 1e6, 1)}
