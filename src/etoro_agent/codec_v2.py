from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import isfinite
from typing import Any, Union, get_args, get_origin, get_type_hints


def decode_value(annotation: Any, value: Any) -> Any:
    if annotation is Any:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, types.UnionType}:
        permits_none = type(None) in args
        if value is None:
            if permits_none:
                return None
            raise TypeError("null is not allowed for this field")
        non_none = [item for item in args if item is not type(None)]
        last: Exception | None = None
        for item in non_none:
            try:
                return decode_value(item, value)
            except (ValueError, TypeError) as exc:
                last = exc
        if last:
            raise last
        return value
    if value is None:
        raise TypeError("null is not allowed for this field")
    if annotation is Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise TypeError("decimal field has an invalid type")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("decimal field is invalid") from exc
        if not parsed.is_finite():
            raise ValueError("decimal field must be finite")
        return parsed
    if annotation is datetime:
        if isinstance(value, datetime):
            parsed_datetime = value
        elif isinstance(value, str):
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise TypeError("datetime field must be an ISO-8601 string")
        if parsed_datetime.tzinfo is None:
            raise ValueError("decoded datetime must be timezone-aware")
        return parsed_datetime
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise TypeError("tuple field must be an array")
        child = args[0] if args else Any
        return tuple(decode_value(child, item) for item in value)
    if origin is list:
        if not isinstance(value, list):
            raise TypeError("list field must be an array")
        child = args[0] if args else Any
        return [decode_value(child, item) for item in value]
    if origin in {dict, Mapping}:
        if not isinstance(value, Mapping):
            raise TypeError("mapping field must be an object")
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        return {
            decode_value(key_type, key): decode_value(value_type, item)
            for key, item in value.items()
        }
    if isinstance(annotation, type) and is_dataclass(annotation):
        return decode_dataclass(annotation, value)
    if annotation is bool:
        if type(value) is not bool:
            raise TypeError("boolean field must be a JSON boolean")
        return value
    if annotation is int:
        if type(value) is not int:
            raise TypeError("integer field must be a JSON integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("float field must be a JSON number")
        parsed_float = float(value)
        if not isfinite(parsed_float):
            raise ValueError("float field must be finite")
        return parsed_float
    if annotation is str:
        if not isinstance(value, str):
            raise TypeError("string field must be a JSON string")
        return value
    return value


def decode_dataclass(cls: type[Any], value: Mapping[str, Any]) -> Any:
    if not is_dataclass(cls) or not isinstance(value, Mapping):
        raise TypeError("decode_dataclass requires a dataclass and mapping")
    hints = get_type_hints(cls)
    declared = {field.name for field in fields(cls)}
    unknown = sorted(set(value) - declared)
    if unknown:
        raise ValueError(f"unknown fields for {cls.__name__}: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for field in fields(cls):
        if field.name not in value:
            if field.default is MISSING and field.default_factory is MISSING:
                raise ValueError(f"missing required field for {cls.__name__}: {field.name}")
            continue
        kwargs[field.name] = decode_value(hints.get(field.name, Any), value[field.name])
    return cls(**kwargs)
