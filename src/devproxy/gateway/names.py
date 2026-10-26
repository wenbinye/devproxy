"""Route name validation and domain assembly."""

from __future__ import annotations

import re

# Use \A / \Z instead of ^ / $ -- $ also matches before a trailing newline,
# so "codeup\n" would be let through by mistake. Both ends must be
# letters/digits: aligned with HOSTNAME_RE / DNS_LABEL_RE, otherwise "foo-"
# passes all entry validation and only fails at render time because
# "foo-.test" is invalid.
NAME_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z")
MAX_NAME_LEN = 63


class InvalidNameError(ValueError):
    def __init__(self, name: str, reason: str):
        super().__init__(f"invalid route name {name!r}: {reason}")
        self.name = name
        self.reason = reason


def validate_name(name: str) -> str:
    """Return the name unchanged if valid; raise InvalidNameError otherwise."""
    if not name:
        raise InvalidNameError(name, "must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise InvalidNameError(name, f"length must not exceed {MAX_NAME_LEN}")
    if not NAME_RE.match(name):
        raise InvalidNameError(
            name, "must be a single label [a-z0-9-] with letters or digits at both ends and no dots"
        )
    return name


def domain_for(name: str, suffix: str) -> str:
    """<name>.<suffix>; a leading dot in suffix is stripped."""
    validate_name(name)
    return f"{name}.{suffix.lstrip('.')}"
