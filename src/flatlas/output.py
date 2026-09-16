"""Stable JSON and CSV row serialization."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from flatlas.errors import FlatlasError


def export_rows(rows: Iterable[dict[str, Any]], fmt: str, output: Path | None = None) -> str:
    materialized = list(rows)
    if fmt == "json":
        rendered = json.dumps(materialized, ensure_ascii=False, indent=2, default=str) + "\n"
    elif fmt == "csv":
        import csv
        import io
        buffer = io.StringIO(newline="")
        keys = list(materialized[0].keys()) if materialized else []
        writer = csv.DictWriter(buffer, fieldnames=keys)
        if keys:
            writer.writeheader()
            writer.writerows(materialized)
        rendered = buffer.getvalue()
    else:
        raise FlatlasError("format must be json or csv")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")
    return rendered
