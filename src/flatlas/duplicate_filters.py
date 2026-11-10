"""Shared duplicate-query filters; never alter scan coverage or path state."""

from dataclasses import dataclass

from flatlas.errors import FlatlasError


@dataclass(frozen=True)
class DuplicateFilters:
    min_size: int = 0
    max_size: int | None = None
    min_count: int = 2
    min_savings: int = 0
    limit: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("min-size", self.min_size), ("max-size", self.max_size)):
            if value is not None and not 0 <= value <= 2**63 - 1:
                raise FlatlasError(f"--{name} must be between 0 and {2**63 - 1} bytes")
        if self.max_size is not None and self.min_size > self.max_size:
            raise FlatlasError("--min-size must not exceed --max-size")
        if self.min_count < 2:
            raise FlatlasError("--min-count must be at least 2")
        if self.min_savings < 0:
            raise FlatlasError("--min-savings must be nonnegative")
        if self.limit is not None and self.limit < 1:
            raise FlatlasError("--limit must be at least 1")

    @property
    def size_bounds(self) -> tuple[int, int]:
        return self.min_size, self.max_size if self.max_size is not None else 2**63 - 1
