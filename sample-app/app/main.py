"""The ASGI application: ``uvicorn app.main:app`` from the ``sample-app`` directory."""

from fastapi import FastAPI

from . import orders, payments


def create_app() -> FastAPI:
    application = FastAPI(title="Commerce Orders & Refunds", version="1.0.0")
    application.include_router(orders.router)
    application.include_router(payments.router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
