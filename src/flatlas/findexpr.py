"""GNU ``find`` expressions evaluated against indexed entries.

Supported subset (anything else is an error, never silently ignored):

- tests: ``-name``, ``-iname``, ``-path``/``-wholename``, ``-ipath``/``-iwholename``,
  ``-type`` (``f d l p s c b``, comma lists allowed), ``-size [+-]N[cwbkMG]``,
  ``-mtime [+-]N``, ``-true``, ``-false``; flatlas adds ``-entries [+-]N`` and
  ``-status WORD[,WORD]``;
- actions: ``-print``, ``-print0``, ``-prune``;
- operators: ``( EXPR )``, ``!``/``-not``, ``-a``/``-and`` (also implicit), ``-o``/``-or``;
- global options: ``-maxdepth N``, ``-mindepth N``.

As in GNU find, an expression without ``-print``/``-print0`` prints every entry
for which it is true, and ``-prune`` stops descending into a matched directory.
Patterns use shell matching (``fnmatch``); ``*`` also matches ``/`` in ``-path``.

``-size`` and ``-entries`` read a directory's indexed subtree totals, not the
directory inode itself as GNU does; an unknown value never matches.
"""

from __future__ import annotations

import re
import stat as stat_module
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from flatlas.errors import FlatlasError


@dataclass(frozen=True)
class FindEntry:
    path: str
    name: str
    kind: str
    mode: int | None
    depth: int
    # Index metadata, only filled in when the expression or the output needs it.
    size: int | None = None
    entries: int | None = None
    mtime_ns: int | None = None
    status: Callable[[], str] | None = None


@dataclass
class FindContext:
    """Per-entry side effects of the expression."""

    output: list[tuple[str, str]] = field(default_factory=list)
    prune: bool = False


Predicate = Callable[[FindEntry, FindContext], bool]


@dataclass(frozen=True)
class FindExpression:
    evaluate: Predicate
    max_depth: int | None
    min_depth: int
    needs_metadata: bool = False


_TYPE_LETTERS = {
    "f": lambda kind, mode: kind == "file",
    "d": lambda kind, mode: kind in {"root", "directory"},
    "l": lambda kind, mode: kind == "symlink",
    "p": lambda kind, mode: mode is not None and stat_module.S_ISFIFO(mode),
    "s": lambda kind, mode: mode is not None and stat_module.S_ISSOCK(mode),
    "c": lambda kind, mode: mode is not None and stat_module.S_ISCHR(mode),
    "b": lambda kind, mode: mode is not None and stat_module.S_ISBLK(mode),
}


def is_expression_start(token: str) -> bool:
    """GNU rule: start points end at the first token that begins an expression."""
    return token.startswith("-") or token in {"!", "(", ")", ","}


def parse_find_expression(tokens: list[str]) -> FindExpression:
    parser = _Parser(tokens)
    if not parser.tokens:
        node: Predicate = _always(True)
    else:
        node = parser.parse_or()
        if parser.position < len(parser.tokens):
            raise FlatlasError(f"find: unexpected '{parser.tokens[parser.position]}'")
    if not parser.has_action:
        node = _print_when(node)
    return FindExpression(node, parser.max_depth, parser.min_depth, parser.needs_metadata)


def _print_when(inner: Predicate) -> Predicate:
    """GNU's implicit ``( EXPR ) -print`` for expressions without an action."""

    def evaluate(entry: FindEntry, context: FindContext) -> bool:
        if inner(entry, context):
            context.output.append((entry.path, "\n"))
        return True

    return evaluate


def _always(value: bool) -> Predicate:
    return lambda _entry, _context: value


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = list(tokens)
        self.position = 0
        self.has_action = False
        self.max_depth: int | None = None
        self.min_depth = 0
        self.needs_metadata = False
        # GNU find measures -mtime ages from when it started.
        self.now_ns = time.time_ns()

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> str:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def _argument(self, option: str) -> str:
        if self._peek() is None:
            raise FlatlasError(f"find: missing argument to '{option}'")
        return self._take()

    def parse_or(self) -> Predicate:
        left = self.parse_and()
        while self._peek() in {"-o", "-or"}:
            self._take()
            right = self.parse_and()
            left = _either(left, right)
        return left

    def parse_and(self) -> Predicate:
        left = self.parse_unary()
        while (token := self._peek()) is not None and token not in {"-o", "-or", ")"}:
            if token in {"-a", "-and"}:
                self._take()
            right = self.parse_unary()
            left = _both(left, right)
        return left

    def parse_unary(self) -> Predicate:
        token = self._peek()
        if token is None:
            raise FlatlasError("find: expected an expression")
        if token in {"!", "-not"}:
            self._take()
            inner = self.parse_unary()
            return lambda entry, context: not inner(entry, context)
        if token == "(":
            self._take()
            inner = self.parse_or()
            if self._peek() != ")":
                raise FlatlasError("find: missing ')'")
            self._take()
            return inner
        if token in {"-a", "-and", "-o", "-or", ")"}:
            raise FlatlasError(f"find: '{token}' without a preceding expression")
        return self.parse_primary()

    def parse_primary(self) -> Predicate:
        token = self._take()
        if token in {"-name", "-iname"}:
            return _matcher(self._argument(token), token == "-iname", lambda entry: entry.name)
        if token in {"-path", "-wholename", "-ipath", "-iwholename"}:
            fold = token in {"-ipath", "-iwholename"}
            return _matcher(self._argument(token), fold, lambda entry: entry.path)
        if token == "-type":
            letters = self._argument(token).split(",")
            unknown = [letter for letter in letters if letter not in _TYPE_LETTERS]
            if unknown or not letters:
                raise FlatlasError(f"find: unknown -type '{','.join(unknown) or ''}' (use f, d, l, p, s, c or b)")
            checks = [_TYPE_LETTERS[letter] for letter in letters]
            return lambda entry, _context: any(check(entry.kind, entry.mode) for check in checks)
        if token == "-size":
            sign, count, unit = _size_argument(self._argument(token))
            self.needs_metadata = True
            # GNU rounds the size up to whole units before comparing.
            return _numeric(sign, count, lambda entry: None if entry.size is None else -(-entry.size // unit))
        if token == "-entries":
            sign, count = _signed_count(token, self._argument(token))
            self.needs_metadata = True
            return _numeric(sign, count, lambda entry: entry.entries)
        if token == "-mtime":
            sign, count = _signed_count(token, self._argument(token))
            self.needs_metadata = True
            now = self.now_ns
            return _numeric(
                sign, count, lambda entry: None if entry.mtime_ns is None else (now - entry.mtime_ns) // _DAY_NS
            )
        if token == "-status":
            words = self._argument(token).split(",")
            unknown = [word for word in words if word not in _STATUSES]
            if unknown or not words:
                raise FlatlasError(f"find: unknown -status '{','.join(unknown)}' (use ok, new or part)")
            self.needs_metadata = True
            wanted = set(words)
            return lambda entry, _context: entry.status is not None and entry.status() in wanted
        if token in {"-true", "-false"}:
            return _always(token == "-true")
        if token in {"-print", "-print0"}:
            self.has_action = True
            terminator = "\n" if token == "-print" else "\0"

            def emit(entry: FindEntry, context: FindContext) -> bool:
                context.output.append((entry.path, terminator))
                return True

            return emit
        if token == "-prune":

            def prune(entry: FindEntry, context: FindContext) -> bool:
                if entry.kind in {"root", "directory"}:
                    context.prune = True
                return True

            return prune
        if token in {"-maxdepth", "-mindepth"}:
            value = self._argument(token)
            if not value.isdigit():
                raise FlatlasError(f"find: {token} needs a non-negative integer, got '{value}'")
            if token == "-maxdepth":
                self.max_depth = int(value)
            else:
                self.min_depth = int(value)
            return _always(True)
        raise FlatlasError(f"find: unsupported expression '{token}'")


_DAY_NS = 86_400 * 1_000_000_000
_STATUSES = {"ok", "new", "part"}
_SIZE_UNITS = {"c": 1, "w": 2, "b": 512, "k": 1024, "M": 1024**2, "G": 1024**3}
_SIZE = re.compile(r"([+-]?)(\d+)([cwbkMG]?)")


def _size_argument(value: str) -> tuple[str, int, int]:
    match = _SIZE.fullmatch(value)
    if match is None:
        raise FlatlasError(f"find: invalid -size '{value}' (use [+-]N with an optional c, w, b, k, M or G)")
    return match.group(1), int(match.group(2)), _SIZE_UNITS[match.group(3) or "b"]


def _signed_count(option: str, value: str) -> tuple[str, int]:
    if not re.fullmatch(r"[+-]?\d+", value):
        raise FlatlasError(f"find: {option} needs [+-]N, got '{value}'")
    return (value[0], int(value[1:])) if value[0] in "+-" else ("", int(value))


def _numeric(sign: str, count: int, value_of: Callable[[FindEntry], int | None]) -> Predicate:
    """GNU numeric test: +N more than N, -N less than N, N exactly; unknown never matches."""

    def test(entry: FindEntry, _context: FindContext) -> bool:
        value = value_of(entry)
        if value is None:
            return False
        return value > count if sign == "+" else value < count if sign == "-" else value == count

    return test


def _matcher(pattern: str, fold: bool, field_of: Callable[[FindEntry], str]) -> Predicate:
    expected = pattern.casefold() if fold else pattern

    def match(entry: FindEntry, _context: FindContext) -> bool:
        value = field_of(entry)
        return fnmatchcase(value.casefold() if fold else value, expected)

    return match


def _either(left: Predicate, right: Predicate) -> Predicate:
    return lambda entry, context: left(entry, context) or right(entry, context)


def _both(left: Predicate, right: Predicate) -> Predicate:
    return lambda entry, context: left(entry, context) and right(entry, context)
