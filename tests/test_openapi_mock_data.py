from pathlib import Path

import yaml

from src.services.openapi_mock_data import build_mock_scenarios, generate_schema_value


def test_schema_value_prefers_example_then_enum_then_default() -> None:
    spec = {"openapi": "3.0.3", "info": {}, "paths": {}}

    assert (
        generate_schema_value(
            {"type": "string", "example": "from-example", "enum": ["first"]},
            openapi_spec=spec,
            seed=0,
        )
        == "from-example"
    )
    assert (
        generate_schema_value(
            {"type": "string", "enum": ["first", "second"]},
            openapi_spec=spec,
            seed=0,
        )
        == "first"
    )
    assert (
        generate_schema_value(
            {"type": "integer", "default": 4},
            openapi_spec=spec,
            seed=0,
        )
        == 4
    )


def test_schema_value_resolves_refs_and_respects_constraints() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {},
        "paths": {},
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "required": ["name", "age"],
                    "properties": {
                        "name": {"type": "string", "minLength": 3},
                        "age": {"type": "integer", "minimum": 18},
                    },
                }
            }
        },
    }

    value = generate_schema_value(
        {"$ref": "#/components/schemas/User"},
        openapi_spec=spec,
        seed=3,
    )

    assert len(value["name"]) >= 3
    assert value["age"] >= 18


def test_schema_value_generates_arrays_and_composition_deterministically() -> None:
    spec = {"openapi": "3.0.3", "info": {}, "paths": {}}

    array_value = generate_schema_value(
        {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": {"type": "integer", "minimum": 3},
            },
        },
        openapi_spec=spec,
        seed=5,
    )
    composed_value = generate_schema_value(
        {
            "anyOf": [
                {"type": "integer", "minimum": 2},
                {"type": "string", "pattern": "^[A-Z]{2}$"},
            ]
        },
        openapi_spec=spec,
        seed=5,
    )
    nullable_value = generate_schema_value(
        {"nullable": True},
        openapi_spec=spec,
        seed=5,
    )

    assert len(array_value) >= 2
    assert array_value[0]["additional_property"] >= 3
    assert composed_value >= 2
    assert nullable_value is None


def test_build_mock_scenarios_generates_request_arguments_and_http_responses() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Users API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "post": {
                    "operationId": "createUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        },
                        {
                            "name": "mode",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "enum": ["sync", "async"]},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string", "minLength": 3},
                                        "email": {"type": "string", "format": "email"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 10},
                                            "email": {"type": "string", "format": "email"},
                                        },
                                    }
                                }
                            },
                        },
                        "400": {
                            "description": "Bad request",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["code"],
                                        "properties": {
                                            "code": {"type": "string", "enum": ["bad_request"]}
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            },
            "/users/{userId}/archive": {
                "delete": {
                    "operationId": "archiveUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "204": {"description": "Archived"}
                    },
                }
            },
        },
    }

    scenarios = build_mock_scenarios(spec, seed=7)

    create_success = next(
        scenario
        for scenario in scenarios
        if scenario.tool_name == "createUser"
        and scenario.scenario_kind == "success"
    )
    create_error = next(
        scenario
        for scenario in scenarios
        if scenario.tool_name == "createUser"
        and scenario.scenario_kind == "http_error"
    )
    archive_success = next(
        scenario
        for scenario in scenarios
        if scenario.tool_name == "archiveUser"
        and scenario.scenario_kind == "success"
    )

    assert create_success.tool_arguments["user_id"] >= 1
    assert create_success.tool_arguments["mode"] == "sync"
    assert len(create_success.tool_arguments["body"]["name"]) >= 3
    assert create_success.tool_arguments["body"]["email"] == "user@example.com"
    assert create_success.status_code == 201
    assert create_success.response_headers == {"content-type": "application/json"}
    assert create_success.response_body["id"] >= 10
    assert create_success.response_body["email"] == "user@example.com"
    assert create_success.validation_status == "ready"

    assert create_error.status_code == 400
    assert create_error.scenario_kind == "http_error"
    assert create_error.response_body == {"code": "bad_request"}
    assert create_error.validation_status == "ready"

    assert archive_success.status_code == 204
    assert archive_success.response_body is None
    assert archive_success.validation_status == "ready"


def test_same_seed_produces_identical_scenarios() -> None:
    spec = yaml.safe_load(
        Path("examples/minimal_users_api.yaml").read_text(encoding="utf-8")
    )

    first = build_mock_scenarios(spec, seed=11)
    second = build_mock_scenarios(spec, seed=11)

    assert first == second


def test_missing_response_schema_produces_unvalidated_scenario() -> None:
    spec = yaml.safe_load(
        Path("examples/minimal_users_api.yaml").read_text(encoding="utf-8")
    )

    scenarios = build_mock_scenarios(spec, seed=11)

    assert len(scenarios) == 1
    assert scenarios[0].validation_status == "unvalidated"
    assert scenarios[0].validation_reason == "response schema is not documented"


def test_unsupported_schema_generation_marks_scenario_unvalidated() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Pattern API", "version": "1.0.0"},
        "paths": {
            "/codes": {
                "get": {
                    "operationId": "getCodes",
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "string",
                                        "pattern": "^[A-Z]{2}$",
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    scenarios = build_mock_scenarios(spec, seed=2)

    assert len(scenarios) == 1
    assert scenarios[0].validation_status == "unvalidated"
    assert "unsupported pattern" in scenarios[0].validation_reason


def test_nested_unsupported_array_item_preserves_validation_path() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Nested API", "version": "1.0.0"},
        "paths": {
            "/widgets": {
                "get": {
                    "operationId": "listWidgets",
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["items"],
                                        "properties": {
                                            "items": {
                                                "type": "array",
                                                "items": {
                                                    "description": "schema type omitted"
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    scenarios = build_mock_scenarios(spec, seed=13)

    assert len(scenarios) == 1
    assert scenarios[0].validation_status == "unvalidated"
    assert (
        scenarios[0].validation_reason
        == "$.paths./widgets.get[0].responses.200.content.application/json.schema/items/0: schema type is not documented"
    )