"""Load eval cases and export a real day as a case."""

import json
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from ..graph import day_window
from ..store import Store
from .models import Case


class CaseError(Exception):
    """A case file is unusable. The message names the file and the field, never the field's value."""


def load_case(path: Path) -> Case:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CaseError(f"{path.name}: cannot be read ({error.strerror or 'I/O error'})") from None
    except ValueError:
        raise CaseError(f"{path.name}: not valid JSON") from None
    try:
        case = Case.model_validate(data)
    except ValidationError as error:
        first = error.errors(include_input=False)[0]
        field = ".".join(str(part) for part in first["loc"]) or "case"
        raise CaseError(f"{path.name}: {field}: {first['msg'].removeprefix('Value error, ')}") from None
    if path.stem != case.id:
        raise CaseError(f"{path.name}: id: must match the file name ({path.stem!r})")
    return case


def load_cases(*directories: Path) -> list[Case]:
    """Every `*.json` in the directories, sorted by id. A missing directory is empty."""
    cases = [load_case(p) for d in directories if d.is_dir() for p in sorted(d.glob("*.json"))]
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise CaseError("two case files have the same id")
    return sorted(cases, key=lambda c: c.id)


def snapshot_day(store: Store, target: date, out_dir: Path) -> Path:
    """Export one day's items (seen or not) as a case file. No model call. Raises CaseError for an empty day."""
    start, end = day_window(target)
    items = store.items_published_between(start, end, include_seen=True)
    if not items:
        raise CaseError(f"no stored items for {target.isoformat()}")
    case = Case(
        id=f"real-{target.isoformat()}", category="real",
        description=f"{len(items)} items stored for {target.isoformat()}, exported from the local database",
        target_date=target, items=items,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{case.id}.json"
    path.write_text(case.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
