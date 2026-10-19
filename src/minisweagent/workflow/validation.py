"""A small JSON-Schema validator for typed workflow results.

ZCode synthesizes a schema from a TypeScript type. This port accepts a JSON
schema directly (from YAML) and validates the subset that workflow authors
actually need: objects, arrays, primitives, enums, unions, and common
constraints.
"""

from __future__ import annotations

import re
from typing import Any


def validate_value(value: Any, schema: dict | None, path: str = "$") -> list[str]:
    if not schema:
        return []
    violations: list[str] = []

    if "anyOf" in schema or "oneOf" in schema:
        branches = schema.get("anyOf") or schema.get("oneOf") or []
        branch_errors = [validate_value(value, branch, path) for branch in branches]
        valid = [errors for errors in branch_errors if not errors]
        if not valid:
            violations.append(f"{path}: expected a value matching one of the schema branches")
        return violations

    if "$ref" in schema:
        # Local $refs are not needed for the built-in workflows; reject clearly.
        return [f"{path}: $ref is not supported in workflow result schemas"]

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_type(value, item) for item in types):
            violations.append(f"{path}: expected {expected_type}, got {_type_name(value)}")
            return violations

    if "const" in schema and value != schema["const"]:
        violations.append(f"{path}: expected {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        violations.append(f"{path}: expected one of {schema['enum']!r}, got {value!r}")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for required in schema.get("required") or []:
            if required not in value:
                violations.append(f"{path}: missing required property {required!r}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in properties:
                violations.extend(validate_value(item, properties[key], child))
            elif additional is False:
                violations.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, dict):
                violations.extend(validate_value(item, additional, child))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            violations.append(f"{path}: expected at least {schema['minItems']} items, got {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            violations.append(f"{path}: expected at most {schema['maxItems']} items, got {len(value)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                violations.extend(validate_value(item, item_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            violations.append(f"{path}: expected at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            violations.append(f"{path}: expected at most {schema['maxLength']} characters")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            violations.append(f"{path}: value does not match pattern {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            violations.append(f"{path}: expected >= {schema['minimum']}, got {value}")
        if "maximum" in schema and value > schema["maximum"]:
            violations.append(f"{path}: expected <= {schema['maximum']}, got {value}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            violations.append(f"{path}: expected > {schema['exclusiveMinimum']}, got {value}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            violations.append(f"{path}: expected < {schema['exclusiveMaximum']}, got {value}")

    return violations


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
