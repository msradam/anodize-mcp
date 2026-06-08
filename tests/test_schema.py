import datetime as dt
import enum
import unittest
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal, Optional, Union

from anodize_mcp import Field
from anodize_mcp.exceptions import InvalidParams
from anodize_mcp.schema import (
    build_input_schema,
    build_params,
    coerce_arguments,
    output_schema_for,
    type_to_schema,
)


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


@dataclass
class Point:
    x: int
    y: int = 0


class TestTypeToSchema(unittest.TestCase):
    def test_primitives(self):
        self.assertEqual(type_to_schema(str), {"type": "string"})
        self.assertEqual(type_to_schema(int), {"type": "integer"})
        self.assertEqual(type_to_schema(float), {"type": "number"})
        self.assertEqual(type_to_schema(bool), {"type": "boolean"})

    def test_list_and_dict(self):
        self.assertEqual(type_to_schema(list[str]), {"type": "array", "items": {"type": "string"}})
        self.assertEqual(
            type_to_schema(dict[str, int]),
            {"type": "object", "additionalProperties": {"type": "integer"}},
        )

    def test_optional(self):
        schema = type_to_schema(Optional[int])
        self.assertEqual(schema, {"anyOf": [{"type": "integer"}, {"type": "null"}]})

    def test_union(self):
        schema = type_to_schema(Union[int, str])
        self.assertEqual(schema, {"anyOf": [{"type": "integer"}, {"type": "string"}]})

    def test_literal(self):
        self.assertEqual(type_to_schema(Literal["a", "b"]), {"enum": ["a", "b"], "type": "string"})

    def test_enum(self):
        self.assertEqual(type_to_schema(Color), {"enum": ["red", "green"], "type": "string"})

    def test_dataclass(self):
        schema = type_to_schema(Point)
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["x"])
        self.assertIn("y", schema["properties"])

    def test_field_metadata(self):
        schema = type_to_schema(Annotated[int, Field(ge=1, le=10, description="count")])
        self.assertEqual(schema["minimum"], 1)
        self.assertEqual(schema["maximum"], 10)
        self.assertEqual(schema["description"], "count")

    def test_stdlib_types(self):
        self.assertEqual(type_to_schema(dt.datetime)["format"], "date-time")
        self.assertEqual(type_to_schema(uuid.UUID)["format"], "uuid")


class TestBuildSchema(unittest.TestCase):
    def test_input_schema_required(self):
        def f(a: int, b: str = "x"):
            return None

        specs = build_params(f)
        schema = build_input_schema(specs)
        self.assertEqual(schema["required"], ["a"])
        self.assertIn("b", schema["properties"])
        self.assertFalse(schema["additionalProperties"])

    def test_output_schema_wraps_non_objects(self):
        # Object-shaped returns map directly; everything else wraps under result.
        dc_schema, dc_wrap = output_schema_for(Point)
        self.assertEqual(dc_schema["type"], "object")
        self.assertFalse(dc_wrap)

        str_schema, str_wrap = output_schema_for(str)
        self.assertTrue(str_wrap)
        self.assertEqual(str_schema["properties"]["result"], {"type": "string"})

        none_schema, none_wrap = output_schema_for(type(None))
        self.assertIsNone(none_schema)


class TestCoerce(unittest.TestCase):
    def _specs(self, func):
        return build_params(func)

    def test_basic(self):
        def f(a: int, b: float, c: bool, d: str):
            return None

        out = coerce_arguments(self._specs(f), {"a": 1, "b": 2, "c": True, "d": "x"})
        self.assertEqual(out, {"a": 1, "b": 2.0, "c": True, "d": "x"})

    def test_numeric_string_coercion(self):
        def f(a: int):
            return None

        self.assertEqual(coerce_arguments(self._specs(f), {"a": "42"}), {"a": 42})

    def test_bool_not_int(self):
        def f(a: int):
            return None

        with self.assertRaises(InvalidParams):
            coerce_arguments(self._specs(f), {"a": True})

    def test_missing_required(self):
        def f(a: int):
            return None

        with self.assertRaises(InvalidParams):
            coerce_arguments(self._specs(f), {})

    def test_default_applied(self):
        def f(a: int = 5):
            return None

        self.assertEqual(coerce_arguments(self._specs(f), {}), {"a": 5})

    def test_list_items(self):
        def f(items: list[int]):
            return None

        self.assertEqual(
            coerce_arguments(self._specs(f), {"items": ["1", 2, 3]}), {"items": [1, 2, 3]}
        )

    def test_enum(self):
        def f(c: Color):
            return None

        out = coerce_arguments(self._specs(f), {"c": "red"})
        self.assertIs(out["c"], Color.RED)

    def test_literal_invalid(self):
        def f(mode: Literal["a", "b"]):
            return None

        with self.assertRaises(InvalidParams):
            coerce_arguments(self._specs(f), {"mode": "c"})

    def test_optional_none(self):
        def f(a: Optional[int] = None):
            return None

        self.assertEqual(coerce_arguments(self._specs(f), {"a": None}), {"a": None})

    def test_dataclass(self):
        def f(p: Point):
            return None

        out = coerce_arguments(self._specs(f), {"p": {"x": 3}})
        self.assertEqual(out["p"], Point(3, 0))

    def test_datetime(self):
        def f(when: dt.datetime):
            return None

        out = coerce_arguments(self._specs(f), {"when": "2025-01-01T00:00:00Z"})
        self.assertEqual(out["when"].year, 2025)

    def test_constraint_violation(self):
        def f(n: Annotated[int, Field(ge=0)]):
            return None

        with self.assertRaises(InvalidParams):
            coerce_arguments(self._specs(f), {"n": -1})

    def test_unknown_args_rejected(self):
        def f(a: int):
            return None

        with self.assertRaises(InvalidParams):
            coerce_arguments(self._specs(f), {"a": 1, "extra": "nope"})

    def test_bool_accepts_zero_one(self):
        def f(flag: bool):
            return None

        self.assertEqual(coerce_arguments(self._specs(f), {"flag": 0}), {"flag": False})
        self.assertEqual(coerce_arguments(self._specs(f), {"flag": 1}), {"flag": True})


class _FakeGe:  # mimics annotated_types.Ge
    def __init__(self, v):
        self.ge = v


class _FakeMinLen:  # mimics annotated_types.MinLen
    def __init__(self, v):
        self.min_length = v


class _FakePydanticField:  # mimics a pydantic v2 FieldInfo
    def __init__(self, constraints, description=None):
        self.metadata = constraints
        self.description = description


class TestConstraintInterop(unittest.TestCase):
    def test_pydantic_fieldinfo_constraints(self):
        def f(n: Annotated[int, _FakePydanticField([_FakeGe(0)], description="count")]):
            return None

        props = build_input_schema(build_params(f))["properties"]
        self.assertEqual(props["n"]["minimum"], 0)
        self.assertEqual(props["n"]["description"], "count")
        with self.assertRaises(InvalidParams):
            coerce_arguments(build_params(f), {"n": -1})

    def test_annotated_types_style_constraints(self):
        def f(s: Annotated[str, _FakeMinLen(2)]):
            return None

        props = build_input_schema(build_params(f))["properties"]
        self.assertEqual(props["s"]["minLength"], 2)
        with self.assertRaises(InvalidParams):
            coerce_arguments(build_params(f), {"s": "x"})

    def test_default_in_schema(self):
        def f(a: int = 7, b: Optional[int] = None):
            return None

        props = build_input_schema(build_params(f))["properties"]
        self.assertEqual(props["a"]["default"], 7)
        self.assertIn("default", props["b"])
        self.assertIsNone(props["b"]["default"])


class TestBareContainers(unittest.TestCase):
    def test_bare_containers_keep_type(self):
        self.assertEqual(type_to_schema(list), {"type": "array"})
        self.assertEqual(type_to_schema(dict), {"type": "object"})
        self.assertEqual(type_to_schema(set), {"type": "array", "uniqueItems": True})


class TestDocstringDescriptions(unittest.TestCase):
    def test_google_args(self):
        def f(x: int, y: str):
            """Do it.

            Args:
                x: the first
                y (str): the second
                    continued
            """

        props = build_input_schema(build_params(f))["properties"]
        self.assertEqual(props["x"]["description"], "the first")
        self.assertEqual(props["y"]["description"], "the second continued")

    def test_rest_param(self):
        def f(n: int):
            """T.

            :param n: a count
            """

        props = build_input_schema(build_params(f))["properties"]
        self.assertEqual(props["n"]["description"], "a count")

    def test_explicit_field_wins_over_docstring(self):
        def f(z: Annotated[int, Field(description="explicit")]):
            """T.

            Args:
                z: from docstring
            """

        props = build_input_schema(build_params(f))["properties"]
        self.assertEqual(props["z"]["description"], "explicit")


if __name__ == "__main__":
    unittest.main()
