"""Consumer-driven contract checks for Runner_Dashboard's Maxwell integration.

Runner_Dashboard records the Maxwell fields it must see before it will accept a
payload as canonical. Maxwell owns the OpenAPI producer contract, so this test
replays that consumer fixture against the generated snapshot in MD CI. A
producer-side rename of a required/discriminating field fails here before RD
discovers the drift downstream (#997).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENAPI_SNAPSHOT = _REPO_ROOT / "docs" / "reference" / "openapi.json"
_RD_FIXTURE = _REPO_ROOT / "tests" / "consumer_contracts" / "runner_dashboard_maxwell.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_schema(
    openapi: dict[str, Any], schema: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    ref = schema.get("$ref")
    if not ref:
        return None, schema
    name = ref.rsplit("/", 1)[-1]
    return name, openapi["components"]["schemas"][name]


def _response_schema(
    openapi: dict[str, Any], fixture: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    response = openapi["paths"][fixture["path"]][fixture["method"]]["responses"][fixture["status"]]
    schema = response["content"]["application/json"]["schema"]
    return _resolve_schema(openapi, schema)


def _request_schema(
    openapi: dict[str, Any], fixture: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    request = openapi["paths"][fixture["path"]][fixture["method"]]["requestBody"]
    schema = request["content"]["application/json"]["schema"]
    return _resolve_schema(openapi, schema)


def _assert_consumer_fields(
    schema_name: str | None, schema: dict[str, Any], fixture: dict[str, Any]
) -> None:
    assert schema_name == fixture["schema"]

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    expected_required = set(fixture["required"])
    discriminators = set(fixture.get("discriminators", []))

    assert expected_required <= set(properties), f"{fixture['name']} missing properties"
    assert expected_required <= required, f"{fixture['name']} no longer requires RD fields"
    assert discriminators <= required, f"{fixture['name']} discriminator can silently default"


def test_runner_dashboard_response_fixtures_match_openapi_snapshot() -> None:
    openapi = _load_json(_OPENAPI_SNAPSHOT)
    fixture = _load_json(_RD_FIXTURE)

    for response_fixture in fixture["responses"]:
        schema_name, schema = _response_schema(openapi, response_fixture)
        _assert_consumer_fields(schema_name, schema, response_fixture)

        for nested in response_fixture.get("nested", []):
            nested_schema = schema["properties"][nested["property"]]["items"]
            nested_name, resolved_nested = _resolve_schema(openapi, nested_schema)
            _assert_consumer_fields(nested_name, resolved_nested, nested)


def test_runner_dashboard_request_fixtures_match_openapi_snapshot() -> None:
    openapi = _load_json(_OPENAPI_SNAPSHOT)
    fixture = _load_json(_RD_FIXTURE)

    for request_fixture in fixture["requests"]:
        schema_name, schema = _request_schema(openapi, request_fixture)
        _assert_consumer_fields(schema_name, schema, request_fixture)
