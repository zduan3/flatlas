"""Machine output: JSON Lines and CSV records, and single JSON objects."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from flatlas.errors import FlatlasError

ROW_FORMATS = ("table", "jsonl", "csv")
OBJECT_FORMATS = ("table", "json")


def check_format(fmt: str, allowed: Sequence[str]) -> None:
    if fmt not in allowed:
        raise FlatlasError(f"--format must be one of: {', '.join(allowed)}")


@contextmanager
def _target(output: Path | None) -> Iterator[TextIO]:
    if output is None:
        yield sys.stdout
        sys.stdout.flush()
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        yield handle


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def write_records(
    records: Iterable[dict[str, Any]], fmt: str, fields: Sequence[str], output: Path | None = None
) -> int:
    """Stream ``records`` as JSON Lines or CSV; returns the number written.

    JSON Lines writes each record as it arrives. CSV always writes the header,
    even with no rows, using ``fields`` as the columns; list values are JSON.
    """
    count = 0
    with _target(output) as handle:
        if fmt == "jsonl":
            for record in records:
                handle.write(_json(record) + "\n")
                count += 1
        elif fmt == "csv":
            writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="raise", lineterminator="\n")
            writer.writeheader()
            for record in records:
                writer.writerow({
                    key: _json(value) if isinstance(value, (list, dict)) else value
                    for key, value in record.items()
                })
                count += 1
        else:
            raise FlatlasError(f"--format must be one of: {', '.join(ROW_FORMATS)}")
    return count


def write_object(value: dict[str, Any], output: Path | None = None) -> None:
    """One JSON object, indented for reading."""
    with _target(output) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
