class FlatlasError(Exception):
    """A user-facing domain error."""


class PathError(FlatlasError):
    """A PATH argument cannot be used (not indexed, missing, unreadable).

    The CLI reports it and exits 1, like a per-argument failure; other domain
    errors exit 2.
    """
