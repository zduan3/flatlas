"""GNU ``find`` expressions evaluated against indexed entries.

Supported subset (anything else is an error, never silently ignored):

- tests: ``-name``, ``-iname``, ``-path``/``-wholename``, ``-ipath``/``-iwholename``,
  ``-type`` (``f d l p s c b``, comma lists allowed), ``-true``, ``-false``;
- actions: ``-print``, ``-print0``, ``-prune``;
- operators: ``( EXPR )``, ``!``/``-not``, ``-a``/``-and`` (also implicit), ``-o``/``-or``;
- global options: ``-maxdepth N``, ``-mindepth N``.

As in GNU find, an expression without ``-print``/``-print0`` prints every entry
for which it is true, and ``-prune`` stops descending into a matched directory.
Patterns use shell matching (``fnmatch``); ``*`` also matches ``/`` in ``-path``.
"""

from __future__ import annotations

import stat as stat_module
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
    return FindExpression(node, parser.max_depth, parser.min_depth)


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
