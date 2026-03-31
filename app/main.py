from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.models import CreateBoletoRequest, CreateBoletoResponse
from app.services.efi import EfiAPIError, EfiClient
from app.services.hasura import HasuraAPIError, HasuraClient

settings = get_settings()
app = FastAPI(title=settings.app_name, version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


@app.post("/boletos", response_model=CreateBoletoResponse)
def create_boleto(payload: CreateBoletoRequest):
    efi_client = EfiClient(settings)
    hasura_client = HasuraClient(settings)

    try:
        efi_response = efi_client.create_boleto(payload)
    except EfiAPIError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "stage": "efi",
                "status_code": exc.status_code,
                "efi": exc.body,
            },
        )

    try:
        hasura_response = hasura_client.insert_financeiro_boleto(payload, efi_response)
    except HasuraAPIError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "stage": "hasura",
                "status_code": exc.status_code,
                "efi": efi_response,
                "hasura": exc.body,
            },
        )

    return {
        "ok": True,
        "efi": efi_response,
        "hasura": hasura_response,
    }
