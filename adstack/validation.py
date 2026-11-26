"""Strict, path-aware validation helpers for parsed YAML."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ConfigError

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")


def mapping(value: Any, path: str, *, required: Iterable[str] = (), optional: Iterable[str] = ()) -> dict:
    """Require a mapping with exactly the allowed keys."""
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a mapping")
    required, optional = set(required), set(optional)
    unknown = sorted(set(value) - required - optional)
    if unknown:
        allowed = ", ".join(sorted(required | optional))
        raise ConfigError(f"{path}: unknown key(s) {', '.join(unknown)} (allowed: {allowed})")
    missing = sorted(required - set(value))
    if missing:
        raise ConfigError(f"{path}: missing required key(s) {', '.join(missing)}")
    return value


def sequence(value: Any, path: str, *, minimum: int = 0, maximum: int = 1000) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{path}: expected a list")
    if not minimum <= len(value) <= maximum:
        raise ConfigError(f"{path}: expected {minimum} to {maximum} items, found {len(value)}")
    return value


def text(value: Any, path: str, *, maximum: int = 80) -> str:
    """Require non-empty single-line text without surrounding whitespace."""
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{path}: expected non-empty text without surrounding spaces or control characters")
    if len(value) > maximum:
        raise ConfigError(f"{path}: {value!r} is {len(value)} characters long; the limit is {maximum}")
    return value


def slug(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ConfigError(
            f"{path}: {value!r} is not a valid id; use 1-60 lowercase letters, digits and hyphens, "
            "starting with a letter or digit"
        )
    return value


def money_micros(value: Any, path: str, *, maximum_micros: int) -> int:
    """Convert an amount such as 12.50 to micros (12_500_000), within 0.01..maximum."""
    try:
        amount = Decimal(str(value))
        valid = (
            not isinstance(value, bool)
            and amount.is_finite()
            and amount == amount.quantize(Decimal("0.01"))
            and Decimal("0.01") <= amount
            and amount * 1_000_000 <= maximum_micros
        )
    except (InvalidOperation, ValueError):
        valid = False
    if not valid:
        limit = f"{maximum_micros / 1_000_000:.2f}"
        raise ConfigError(f"{path}: expected an amount from 0.01 to {limit} with at most 2 decimal places")
    return int(amount * 1_000_000)


def distinct(values: list, path: str, key: Callable[[Any], Any] = lambda item: item) -> list:
    seen = set()
    for item in values:
        identity = key(item)
        if identity in seen:
            raise ConfigError(f"{path}: duplicate entry {identity!r}")
        seen.add(identity)
    return values
