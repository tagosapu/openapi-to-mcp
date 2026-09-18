from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .auth import EnvironmentCredentialResolver, JwtAuthorizer
from .limits import RateLimiter
from .mapping import MappingEngine
from .rest_connector import ConnectorRegistry
from .routes import create_router, current_principal, install_exception_handlers
from .settings import TransferSettings as Settings
from .store import SqliteTransferStore, create_payload_protector
from .worker import RetryPolicy, TransferWorker


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
    app.include_router(create_router())
    install_exception_handlers(app)
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