from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


def decode_value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    if annotation is Any:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, types.UnionType}:
        non_none = [item for item in args if item is not type(None)]
        if value is None and len(non_none) != len(args):
            return None
        last: Exception | None = None
        for item in non_none:
            try:
                return decode_value(item, value)
            except (ValueError, TypeError) as exc:
                last = exc
        if last:
            raise last
        return value
    if annotation is Decimal:
        return Decimal(str(value))
    if annotation is datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("decoded datetime must be timezone-aware")
        return parsed
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if origin is tuple:
        child = args[0] if args else Any
        return tuple(decode_value(child, item) for item in value)
    if origin is list:
        child = args[0] if args else Any
        return [decode_value(child, item) for item in value]
    if origin in {dict, Mapping}:
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        return {
            decode_value(key_type, key): decode_value(value_type, item)
            for key, item in value.items()
        }
    if isinstance(annotation, type) and is_dataclass(annotation):
        return decode_dataclass(annotation, value)
    if annotation in {str, int, float, bool}:
        return annotation(value)
    return value


def decode_dataclass(cls: type[Any], value: Mapping[str, Any]) -> Any:
    if not is_dataclass(cls) or not isinstance(value, Mapping):
        raise TypeError("decode_dataclass requires a dataclass and mapping")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for field in fields(cls):
        if field.name not in value:
            continue
        kwargs[field.name] = decode_value(hints.get(field.name, Any), value[field.name])
    return cls(**kwargs)
