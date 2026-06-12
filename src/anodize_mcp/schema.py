"""Generate JSON Schema from type hints and validate/coerce values at runtime.

This is the pure-Python stand-in for what pydantic does in FastMCP. It covers
the types that show up in tool signatures: primitives, ``Optional``/``Union``,
``list``/``dict``/``set``/``tuple``, ``Literal``, ``Enum``, dataclasses, and a
handful of stdlib types (``datetime``, ``date``, ``UUID``, ``Decimal``). It is
deliberately small; it is not a general JSON Schema engine.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import datetime as _dt
import enum
import inspect
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal, Optional

from . import _compat
from .exceptions import InvalidParams

_UNSET: Any = object()

# When set, scalar coercers reject cross-type inputs (e.g. the string "10" for an
# int parameter) instead of parsing them, matching FastMCP's strict_input_validation.
_strict: contextvars.ContextVar[bool] = contextvars.ContextVar("strict_coercion", default=False)


@dataclass
class FieldInfo:
    """Constraints and metadata attached to a parameter via ``Annotated``.

    Use through :func:`Field`, e.g. ``Annotated[int, Field(ge=0, le=100)]``.
    """

    description: Optional[str] = None
    default: Any = _UNSET
    title: Optional[str] = None
    ge: Optional[float] = None
    gt: Optional[float] = None
    le: Optional[float] = None
    lt: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    examples: Optional[list[Any]] = None

    def apply_to_schema(self, schema: dict[str, Any]) -> None:
        mapping = {
            "description": self.description,
            "title": self.title,
            "minimum": self.ge,
            "exclusiveMinimum": self.gt,
            "maximum": self.le,
            "exclusiveMaximum": self.lt,
            "minLength": self.min_length,
            "maxLength": self.max_length,
            "pattern": self.pattern,
            "examples": self.examples,
        }
        for key, value in mapping.items():
            if value is not None:
                schema[key] = value


def Field(  # noqa: N802 - deliberately Field() to read like pydantic
    default: Any = _UNSET,
    *,
    description: Optional[str] = None,
    title: Optional[str] = None,
    ge: Optional[float] = None,
    gt: Optional[float] = None,
    le: Optional[float] = None,
    lt: Optional[float] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
    examples: Optional[list[Any]] = None,
) -> Any:
    return FieldInfo(
        description=description,
        default=default,
        title=title,
        ge=ge,
        gt=gt,
        le=le,
        lt=lt,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        examples=examples,
    )


_CONSTRAINT_ATTRS = ("ge", "gt", "le", "lt", "min_length", "max_length")


def _absorb_constraints(info: FieldInfo, obj: Any) -> None:
    """Pull recognised constraint attributes off ``obj`` into ``info``.

    Works for ``annotated_types`` objects (``Ge``, ``Le``, ``MinLen``, ...) and
    anything else exposing the same attribute names, so ``Annotated[int,
    Field(ge=0)]`` from pydantic and ``Annotated[int, Ge(0)]`` both apply.
    """
    for attr in _CONSTRAINT_ATTRS:
        value = getattr(obj, attr, None)
        if value is not None and getattr(info, attr) is None:
            setattr(info, attr, value)


def _field_from_metadata(metadata: tuple[Any, ...]) -> FieldInfo:
    info = FieldInfo()
    for item in metadata:
        if isinstance(item, FieldInfo):
            return item
        if isinstance(item, str):
            if info.description is None:
                info.description = item
            continue
        # A pydantic FieldInfo exposes its constraints in a `metadata` list and
        # carries an optional `description`; read both without importing pydantic.
        nested = getattr(item, "metadata", None)
        if isinstance(nested, (list, tuple)):
            description = getattr(item, "description", None)
            if isinstance(description, str) and info.description is None:
                info.description = description
            for constraint in nested:
                _absorb_constraints(info, constraint)
        _absorb_constraints(info, item)
    return info


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

_PRIMITIVE_SCHEMAS: dict[Any, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    type(None): {"type": "null"},
    _dt.datetime: {"type": "string", "format": "date-time"},
    _dt.date: {"type": "string", "format": "date"},
    _dt.time: {"type": "string", "format": "time"},
    uuid.UUID: {"type": "string", "format": "uuid"},
    Decimal: {"type": "number"},
    bytes: {"type": "string", "contentEncoding": "base64"},
    # Bare (unsubscripted) container annotations: keep the JSON type even though
    # the item type is unknown.
    list: {"type": "array"},
    tuple: {"type": "array"},
    set: {"type": "array", "uniqueItems": True},
    frozenset: {"type": "array", "uniqueItems": True},
    dict: {"type": "object"},
}


def type_to_schema(tp: Any) -> dict[str, Any]:
    """Build a JSON Schema fragment for a single type annotation."""
    tp, metadata = _compat.unwrap_annotated(tp)
    field = _field_from_metadata(metadata)
    schema = _type_to_schema_inner(tp)
    field.apply_to_schema(schema)
    return schema


def _type_to_schema_inner(tp: Any) -> dict[str, Any]:
    if tp is Any or tp is inspect.Parameter.empty:
        return {}

    if tp in _PRIMITIVE_SCHEMAS:
        return dict(_PRIMITIVE_SCHEMAS[tp])

    # Optional[X] / Union[...]
    if _compat.is_union(tp):
        args = _compat.get_args(tp)
        non_none = [a for a in args if a is not _compat.NoneType]
        sub = [_type_to_schema_inner(a) for a in non_none]
        nullable = len(non_none) != len(args)
        union_schema: dict[str, Any] = sub[0] if len(sub) == 1 else {"anyOf": sub}
        if nullable:
            union_schema = {"anyOf": [union_schema, {"type": "null"}]}
        return union_schema

    # Enum
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        enum_values = [e.value for e in tp]
        enum_schema: dict[str, Any] = {"enum": enum_values}
        enum_type = _infer_enum_type(enum_values)
        if enum_type:
            enum_schema["type"] = enum_type
        return enum_schema

    origin = _compat.get_origin(tp)

    # Literal[...]
    if origin is Literal:
        literal_values = list(_compat.get_args(tp))
        literal_schema: dict[str, Any] = {"enum": literal_values}
        literal_type = _infer_enum_type(literal_values)
        if literal_type:
            literal_schema["type"] = literal_type
        return literal_schema

    # list[X] / set[X] / frozenset[X] / tuple[X, ...]
    if origin in (list, set, frozenset):
        args = _compat.get_args(tp)
        items = _type_to_schema_inner(args[0]) if args else {}
        array_schema: dict[str, Any] = {"type": "array", "items": items}
        if origin in (set, frozenset):
            array_schema["uniqueItems"] = True
        return array_schema

    if origin is tuple:
        args = _compat.get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": _type_to_schema_inner(args[0])}
        if args:
            return {
                "type": "array",
                "prefixItems": [_type_to_schema_inner(a) for a in args],
                "minItems": len(args),
                "maxItems": len(args),
            }
        return {"type": "array"}

    # dict[K, V]
    if origin is dict:
        args = _compat.get_args(tp)
        value_schema = _type_to_schema_inner(args[1]) if len(args) == 2 else {}
        return {"type": "object", "additionalProperties": value_schema or True}

    # Dataclasses become nested objects.
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _dataclass_schema(tp)

    # A pydantic-style model (the server's own choice) carries its own JSON Schema.
    if isinstance(tp, type) and hasattr(tp, "model_json_schema"):
        with contextlib.suppress(Exception):
            return tp.model_json_schema()

    # Unknown: leave unconstrained rather than guess wrongly.
    return {}


def _infer_enum_type(values: list[Any]) -> Optional[str]:
    json_types = {_json_type_name(v) for v in values}
    if len(json_types) == 1:
        return json_types.pop()
    return None


def _json_type_name(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _dataclass_schema(tp: type) -> dict[str, Any]:
    hints = _compat.get_type_hints(tp)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(tp):
        annotation = hints.get(f.name, f.type)
        properties[f.name] = type_to_schema(annotation)
        has_default = (
            f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
        )
        if not has_default:
            required.append(f.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


# ---------------------------------------------------------------------------
# Signature -> input schema
# ---------------------------------------------------------------------------


@dataclass
class ParamSpec:
    name: str
    annotation: Any
    field: FieldInfo
    required: bool
    default: Any


def build_params(func: Callable[..., Any], skip: tuple[str, ...] = ()) -> list[ParamSpec]:
    """Inspect ``func`` and return one :class:`ParamSpec` per accepted argument.

    Parameters named in ``skip`` (e.g. the injected context) are omitted.
    """
    signature = inspect.signature(func)
    hints = _compat.get_type_hints(func)
    param_docs = parse_param_docs(inspect.getdoc(func))
    specs: list[ParamSpec] = []
    for name, param in signature.parameters.items():
        if name in skip:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
        _, metadata = _compat.unwrap_annotated(annotation)
        field = _field_from_metadata(metadata)
        if field.description is None and name in param_docs:
            field.description = param_docs[name]

        if param.default is not inspect.Parameter.empty:
            default = param.default
            required = False
        elif field.default is not _UNSET:
            default = field.default
            required = False
        else:
            default = _UNSET
            required = True

        specs.append(
            ParamSpec(
                name=name,
                annotation=annotation,
                field=field,
                required=required,
                default=default,
            )
        )
    return specs


def build_input_schema(specs: list[ParamSpec]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for spec in specs:
        prop = type_to_schema(spec.annotation)
        # A description from the docstring fills in only when the annotation
        # (Annotated[..., Field(description=...)]) did not supply one.
        if "description" not in prop and spec.field.description:
            prop["description"] = spec.field.description
        if spec.default is not _UNSET and isinstance(
            spec.default, (str, int, float, bool, type(None), list, dict)
        ):
            prop.setdefault("default", spec.default)
        properties[spec.name] = prop
        if spec.required:
            required.append(spec.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_SECTION_RE = re.compile(
    r"^[ \t]*(Args|Arguments|Parameters|Returns?|Raises?|Yields?|Examples?|Notes?)[ \t]*:[ \t]*$",
    re.MULTILINE,
)


def doc_summary(doc: Optional[str]) -> Optional[str]:
    """Return the docstring up to the first ``Args:``/``Returns:``/``:param`` section."""
    if not doc:
        return None
    cut = len(doc)
    section = _SECTION_RE.search(doc)
    if section:
        cut = section.start()
    param = re.search(r"^[ \t]*:param\b", doc, re.MULTILINE)
    if param:
        cut = min(cut, param.start())
    return doc[:cut].strip() or None


def parse_param_docs(doc: Optional[str]) -> dict[str, str]:
    """Extract per-parameter descriptions from a docstring.

    Handles Google style (an ``Args:`` section of ``name: description`` lines,
    with indented continuations) and reStructuredText ``:param name:`` lines.
    """
    if not doc:
        return {}
    out: dict[str, str] = {}
    lines = doc.splitlines()

    for line in lines:
        m = re.match(r"\s*:param\s+(?:\S+\s+)?(\w+)\s*:\s*(.+)", line)
        if m:
            out[m.group(1)] = m.group(2).strip()

    in_args = False
    current: Optional[str] = None
    for line in lines:
        if re.match(r"\s*(Args|Arguments|Parameters)\s*:\s*$", line):
            in_args = True
            current = None
            continue
        if not in_args:
            continue
        if re.match(r"\s*(Returns?|Raises?|Yields?|Examples?|Notes?)\s*:\s*$", line):
            break
        if not line.strip():
            continue
        m = re.match(r"\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)", line)
        if m:
            current = m.group(1)
            out.setdefault(current, m.group(2).strip())
        elif current:
            out[current] = f"{out[current]} {line.strip()}".strip()
    return out


def output_schema_for(return_annotation: Any) -> tuple[Optional[dict[str, Any]], bool]:
    """Return ``(output_schema, wrap)`` for a tool's return annotation.

    Mirrors FastMCP: an object-shaped return (dataclass or ``dict``) maps to its
    own schema; any other annotated return is wrapped under a ``result`` property
    (``wrap`` is then True, so the value is emitted as ``{"result": value}``). No
    annotation, or ``None``, yields ``(None, False)`` and no structured output.
    """
    if return_annotation is inspect.Signature.empty or return_annotation is None:
        return None, False
    tp, _ = _compat.unwrap_annotated(return_annotation)
    if tp is type(None):
        return None, False
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _dataclass_schema(tp), False
    if _compat.get_origin(tp) is dict:
        return type_to_schema(tp), False
    wrapped = {
        "type": "object",
        "properties": {"result": type_to_schema(tp)},
        "required": ["result"],
    }
    return wrapped, True


# ---------------------------------------------------------------------------
# Runtime validation / coercion
# ---------------------------------------------------------------------------


def coerce_arguments(
    specs: list[ParamSpec], arguments: dict[str, Any], *, strict: bool = False
) -> dict[str, Any]:
    """Validate and coerce a raw ``arguments`` dict against parameter specs.

    With ``strict``, scalar values are not parsed across types (the string
    ``"10"`` is rejected for an ``int`` parameter). Raises
    :class:`InvalidParams` on the first problem.
    """
    if not isinstance(arguments, dict):
        raise InvalidParams("arguments must be an object")

    token = _strict.set(strict)
    try:
        return _coerce_arguments(specs, arguments)
    finally:
        _strict.reset(token)


def _coerce_arguments(specs: list[ParamSpec], arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for spec in specs:
        if spec.name in arguments:
            value = arguments[spec.name]
            result[spec.name] = _coerce(value, spec.annotation, path=spec.name)
            _check_constraints(result[spec.name], spec.field, path=spec.name)
        elif spec.required:
            raise InvalidParams(f"missing required argument: {spec.name!r}")
        elif spec.default is not _UNSET:
            result[spec.name] = spec.default

    unknown = set(arguments) - {spec.name for spec in specs}
    if unknown:
        raise InvalidParams(f"unexpected argument(s): {', '.join(sorted(unknown))}")
    return result


def _coerce(value: Any, tp: Any, path: str) -> Any:
    tp, _ = _compat.unwrap_annotated(tp)

    if tp is Any or tp is inspect.Parameter.empty:
        return value

    if _compat.is_union(tp):
        args = _compat.get_args(tp)
        if value is None and _compat.NoneType in args:
            return None
        errors = []
        for arg in args:
            if arg is _compat.NoneType:
                continue
            try:
                return _coerce(value, arg, path)
            except InvalidParams as exc:  # noqa: PERF203
                errors.append(str(exc))
        raise InvalidParams(f"{path}: no union member matched ({'; '.join(errors)})")

    if tp is type(None):
        if value is None:
            return None
        raise InvalidParams(f"{path}: invalid, expected null")

    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return _coerce_enum(value, tp, path)

    origin = _compat.get_origin(tp)

    if origin is Literal:
        allowed = _compat.get_args(tp)
        if value in allowed:
            return value
        raise InvalidParams(f"{path}: {value!r} not one of {list(allowed)!r}")

    if tp is bool:
        return _coerce_bool(value, path)
    if tp is int:
        return _coerce_int(value, path)
    if tp is float:
        return _coerce_float(value, path)
    if tp is str:
        if isinstance(value, str):
            return value
        raise InvalidParams(f"{path}: invalid, expected string")
    if tp is bytes:
        return _coerce_bytes(value, path)
    if tp is Decimal:
        return _coerce_decimal(value, path)
    if tp is _dt.datetime:
        return _coerce_datetime(value, path)
    if tp is _dt.date:
        return _coerce_date(value, path)
    if tp is uuid.UUID:
        return _coerce_uuid(value, path)

    if origin in (list, set, frozenset):
        if not isinstance(value, list):
            raise InvalidParams(f"{path}: invalid, expected array")
        args = _compat.get_args(tp)
        item_type = args[0] if args else Any
        items = [_coerce(v, item_type, f"{path}[{i}]") for i, v in enumerate(value)]
        if origin is set:
            return set(items)
        if origin is frozenset:
            return frozenset(items)
        return items

    if origin is tuple:
        if not isinstance(value, list):
            raise InvalidParams(f"{path}: invalid, expected array")
        args = _compat.get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(v, args[0], f"{path}[{i}]") for i, v in enumerate(value))
        if args:
            if len(value) != len(args):
                raise InvalidParams(f"{path}: invalid, expected {len(args)} items")
            return tuple(_coerce(v, a, f"{path}[{i}]") for i, (v, a) in enumerate(zip(value, args)))
        return tuple(value)

    if origin is dict:
        if not isinstance(value, dict):
            raise InvalidParams(f"{path}: invalid, expected object")
        args = _compat.get_args(tp)
        if len(args) == 2:
            return {k: _coerce(v, args[1], f"{path}.{k}") for k, v in value.items()}
        return dict(value)

    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _coerce_dataclass(value, tp, path)

    # Build a pydantic-style model from the dict, using the server's own pydantic.
    if isinstance(tp, type) and isinstance(value, dict):
        if hasattr(tp, "model_validate"):  # pydantic v2
            return tp.model_validate(value)
        if hasattr(tp, "parse_obj"):  # pydantic v1
            return tp.parse_obj(value)

    return value


def _coerce_bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if not _strict.get():
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
    raise InvalidParams(f"{path}: invalid, expected boolean")


def _coerce_int(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise InvalidParams(f"{path}: invalid, expected integer, got boolean")
    if isinstance(value, int):
        return value
    if not _strict.get():
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
    raise InvalidParams(f"{path}: invalid, expected integer")


def _coerce_float(value: Any, path: str) -> float:
    if isinstance(value, bool):
        raise InvalidParams(f"{path}: invalid, expected number, got boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if not _strict.get() and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise InvalidParams(f"{path}: invalid, expected number")


def _coerce_bytes(value: Any, path: str) -> bytes:
    import base64

    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise InvalidParams(f"{path}: invalid, expected base64 string") from exc
    raise InvalidParams(f"{path}: invalid, expected base64 string")


def _coerce_decimal(value: Any, path: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidParams(f"{path}: invalid, expected decimal") from exc


def _coerce_datetime(value: Any, path: str) -> _dt.datetime:
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidParams(f"{path}: invalid, expected ISO-8601 datetime") from exc
    raise InvalidParams(f"{path}: invalid, expected ISO-8601 datetime")


def _coerce_date(value: Any, path: str) -> _dt.date:
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidParams(f"{path}: invalid, expected ISO-8601 date") from exc
    raise InvalidParams(f"{path}: invalid, expected ISO-8601 date")


def _coerce_uuid(value: Any, path: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise InvalidParams(f"{path}: invalid, expected UUID") from exc
    raise InvalidParams(f"{path}: invalid, expected UUID")


def _coerce_enum(value: Any, tp: type[enum.Enum], path: str) -> Any:
    try:
        return tp(value)
    except ValueError:
        pass
    if isinstance(value, str) and hasattr(tp, value):
        return tp[value]
    raise InvalidParams(f"{path}: {value!r} is not a valid {tp.__name__}")


def _coerce_dataclass(value: Any, tp: type, path: str) -> Any:
    if isinstance(value, tp):
        return value
    if not isinstance(value, dict):
        raise InvalidParams(f"{path}: invalid, expected object")
    hints = _compat.get_type_hints(tp)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(tp):
        annotation = hints.get(f.name, f.type)
        if f.name in value:
            kwargs[f.name] = _coerce(value[f.name], annotation, f"{path}.{f.name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise InvalidParams(f"{path}.{f.name}: missing required field")
    return tp(**kwargs)


def _check_constraints(value: Any, field: FieldInfo, path: str) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if field.ge is not None and value < field.ge:
            raise InvalidParams(f"{path}: must be >= {field.ge}")
        if field.gt is not None and value <= field.gt:
            raise InvalidParams(f"{path}: must be > {field.gt}")
        if field.le is not None and value > field.le:
            raise InvalidParams(f"{path}: must be <= {field.le}")
        if field.lt is not None and value >= field.lt:
            raise InvalidParams(f"{path}: must be < {field.lt}")
    if isinstance(value, (str, list, dict)):
        length = len(value)
        if field.min_length is not None and length < field.min_length:
            raise InvalidParams(f"{path}: length must be >= {field.min_length}")
        if field.max_length is not None and length > field.max_length:
            raise InvalidParams(f"{path}: length must be <= {field.max_length}")
    if field.pattern is not None and isinstance(value, str):
        import re

        if re.search(field.pattern, value) is None:
            raise InvalidParams(f"{path}: does not match pattern {field.pattern!r}")
