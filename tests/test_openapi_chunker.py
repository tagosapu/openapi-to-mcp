from src.services.openapi_chunker import build_openapi_chunks


def test_chunks_operations_with_referenced_component_closure() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1.0.0"},
        "paths": {
            "/users": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/User"}
                                }
                            }
                        }
                    }
                }
            },
            "/health": {
                "get": {
                    "responses": {"200": {"description": "ok"}}
                }
            },
        },
        "components": {
            "securitySchemes": {
                "apiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                }
            },
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {"id": {"$ref": "#/components/schemas/Id"}},
                },
                "Id": {"type": "string"},
                "Unused": {"type": "string"},
            },
        },
    }

    chunks = build_openapi_chunks(
        spec, max_prompt_tokens=2_100, prompt_overhead_tokens=2_000
    )

    operation_chunks = [chunk for chunk in chunks if not chunk.schema_only]
    schema_chunks = [chunk for chunk in chunks if chunk.schema_only]

    assert operation_chunks
    assert set(operation_chunks[0].spec["components"]["schemas"]) == {"User", "Id"}
    assert (
        operation_chunks[0].spec["components"]["securitySchemes"]["apiKey"]["type"]
        == "apiKey"
    )
    assert schema_chunks[0].schema_names == ("Unused",)


def test_chunk_limit_is_applied_to_serialized_spec() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1.0.0"},
        "paths": {
            "/a": {
                "get": {
                    "description": "a" * 200,
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/b": {
                "get": {
                    "description": "b" * 200,
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }

    chunks = build_openapi_chunks(
        spec, max_prompt_tokens=2_140, prompt_overhead_tokens=2_000
    )

    assert len(chunks) == 2
    assert all(chunk.estimated_tokens <= 2_140 for chunk in chunks)