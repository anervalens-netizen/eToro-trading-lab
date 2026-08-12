from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def load_strict_json_object(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("configuration is not valid strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


def strict_object(
    value: object,
    *,
    label: str,
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    required_keys = frozenset(required)
    allowed_keys = required_keys | frozenset(optional)
    actual_keys = frozenset(value)
    missing = sorted(required_keys - actual_keys)
    unknown = sorted(actual_keys - allowed_keys)
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")
    return value


def strict_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def strict_env_bool(value: str, *, label: str) -> bool:
    if value == "1":
        return True
    if value == "0":
        return False
    raise ValueError(f"{label} must be exactly 0 or 1")


def strict_int(
    value: object,
    *,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a JSON integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} is below its minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} exceeds its maximum")
    return value


def strict_decimal(
    value: object,
    *,
    label: str,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{label} must be an exact decimal value")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be an exact decimal value") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{label} must be positive")
    if non_negative and parsed < 0:
        raise ValueError(f"{label} cannot be negative")
    return parsed


def strict_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def strict_list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value
