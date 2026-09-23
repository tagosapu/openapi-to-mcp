from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .auth import EnvironmentCredentialResolver, JwtAuthorizer
from .limits import PayloadLimits, RateLimiter
from .mapping import MappingEngine
from .observability import TransferObservability
from .rest_connector import ConnectorRegistry
from .routes import create_router, current_principal, install_exception_handlers
from .settings import TransferSettings as Settings
from .store import SqliteTransferStore, create_payload_protector
from .worker import RetryPolicy, TransferWorker


_PUBLIC_OPERATION_IDS = {
    ("/v1/transfers", "post"): ("createTransfer", ["transfer:write"]),
    ("/v1/transfers", "get"): ("listTransfers", ["transfer:read"]),
    ("/v1/transfers/{transfer_id}", "get"): ("getTransfer", ["transfer:read"]),
    ("/v1/transfers/{transfer_id}/retry", "post"): ("retryTransfer", ["transfer:retry"]),
    ("/v1/transfers/{transfer_id}/cancel", "post"): ("cancelTransfer", ["transfer:cancel"]),
    ("/v1/transfers/{transfer_id}/review", "post"): ("reviewTransfer", ["transfer:review"]),
    ("/v1/transfers/{transfer_id}/reconcile", "post"): ("reconcileTransfer", ["transfer:reconcile"]),
    ("/v1/mappings/{mapping_id}/preview", "post"): ("previewMapping", ["mapping:read"]),
    ("/v1/connectors", "get"): ("listConnectors", ["connector:read"]),
    ("/v1/connectors", "post"): ("createConnector", ["connector:admin"]),
    ("/v1/connectors/{connector_id}/validate", "post"): ("validateConnector", ["connector:admin"]),
    ("/v1/mappings", "get"): ("listMappings", ["mapping:read"]),
    ("/v1/mappings", "post"): ("createMapping", ["mapping:write"]),
    ("/v1/health/live", "get"): ("getLiveness", []),
    ("/v1/health/ready", "get"): ("getReadiness", []),
}


class _SystemClock:
    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        credential_resolver = EnvironmentCredentialResolver(resolved)
        protector = await create_payload_protector(resolved, credential_resolver)
        store = SqliteTransferStore(
            Path(resolved.database_path),
            protector=protector,
            idempotency_retention=timedelta(hours=resolved.idempotency_retention_hours),
        )
        registry = ConnectorRegistry(store, credential_resolver, resolved)
        mapping_engine = MappingEngine()
        worker = TransferWorker(
            store=store,
            registry=registry,
            mapping_engine=mapping_engine,
            retry_policy=RetryPolicy.from_settings(resolved),
            observability=app.state.observability,
            payload_retention_days=resolved.payload_retention_days,
            audit_retention_days=resolved.audit_retention_days,
        )
        task: asyncio.Task[None] | None = None
        try:
            await store.initialize()
            await worker.recover_inflight()
            app.state.store = store
            app.state.registry = registry
            app.state.worker = worker
            app.state.credential_resolver = credential_resolver
            app.state.mapping_engine = mapping_engine
            await worker.run_maintenance()
            if resolved.worker_enabled:
                task = asyncio.create_task(worker.start())
                await task
            yield
        finally:
            if task is not None:
                await worker.stop()
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            await store.close()

    app = FastAPI(
        title="OCR Transfer Connector API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.authorizer = JwtAuthorizer(resolved)
    app.state.rate_limiter = RateLimiter(
        resolved.requests_per_minute,
        resolved.burst,
        _SystemClock(),
    )
    app.state.payload_limits = PayloadLimits(
        max_payload_bytes=resolved.max_payload_bytes,
    )
    app.state.observability = TransferObservability()
    app.include_router(create_router())
    install_exception_handlers(app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        schema["openapi"] = "3.1.0"
        schema.setdefault("components", {})["securitySchemes"] = {
            "OAuth2": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/oauth/token",
                        "scopes": {
                            scope: "OCR transfer API scope"
                            for _, scopes in _PUBLIC_OPERATION_IDS.values()
                            for scope in scopes
                        },
                    }
                },
            }
        }
        for path, methods in schema.get("paths", {}).items():
            for method, operation in methods.items():
                if method not in {"get", "post", "put", "patch", "delete", "options", "head"}:
                    continue
                metadata = _PUBLIC_OPERATION_IDS.get((path, method))
                if metadata is None:
                    continue
                operation_id, scopes = metadata
                operation["operationId"] = operation_id
                operation["security"] = [] if not scopes else [{"OAuth2": scopes}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OCR transfer connector API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    uvicorn.run(
        "src.transfer.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )


__all__ = ["create_app", "current_principal", "main"]