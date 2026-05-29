from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings, parse_cors_values
from app.logging_utils import sanitize_for_log
from app.models import (
    CreateBoletoRequest,
    CreateBoletoResponse,
    UpdateBoletoDueDateRequest,
    UpdateBoletoMetadataRequest,
)
from app.services.efi import EfiAPIError, EfiClient
from app.services.hasura import HasuraAPIError, HasuraClient

logger = logging.getLogger(__name__)

settings = get_settings()
logging.getLogger("app").setLevel(logging.DEBUG if settings.app_debug else logging.INFO)
app = FastAPI(title=settings.app_name, version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_values(settings.cors_allow_origins),
    allow_methods=parse_cors_values(settings.cors_allow_methods),
    allow_headers=parse_cors_values(settings.cors_allow_headers),
    allow_credentials=settings.cors_allow_credentials,
)


def downstream_error_response(
    stage: str, exc: EfiAPIError | HasuraAPIError, extra: dict[str, Any] | None = None
) -> JSONResponse:
    status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
    logger.error(
        "Downstream error stage=%s http_status=%s downstream_status=%s body=%s",
        stage,
        status_code,
        exc.status_code,
        sanitize_for_log(exc.body),
    )
    content: dict[str, Any] = {
        "ok": False,
        "stage": stage,
        "status_code": exc.status_code,
    }
    if extra:
        content.update(extra)
    content[stage] = exc.body
    return JSONResponse(status_code=status_code, content=content)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


@app.post("/boletos", response_model=CreateBoletoResponse)
def create_boleto(payload: CreateBoletoRequest):
    efi_client = EfiClient(settings)
    hasura_client = HasuraClient(settings)

    logger.info(
        "Create boleto request empresa_id=%s tipo=%s valor=%s vencimento=%s custom_id=%s has_payload_notification_url=%s",
        payload.empresa_id,
        payload.tipo,
        payload.valor,
        payload.vencimento,
        payload.custom_id,
        bool(payload.notification_url),
    )
    try:
        efi_response = efi_client.create_boleto(payload)
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)

    try:
        hasura_response = hasura_client.insert_financeiro_boleto(payload, efi_response)
    except HasuraAPIError as exc:
        return downstream_error_response("hasura", exc, {"efi": efi_response})

    return {
        "ok": True,
        "efi": efi_response,
        "hasura": hasura_response,
    }


@app.get("/boletos/{charge_id}")
def get_boleto(charge_id: int):
    efi_client = EfiClient(settings)
    try:
        efi_response = efi_client.get_charge(charge_id)
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)
    return {"ok": True, "efi": efi_response}


@app.put("/boletos/{charge_id}/cancelar")
def cancel_boleto(charge_id: int):
    efi_client = EfiClient(settings)
    try:
        efi_response = efi_client.cancel_charge(charge_id)
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)
    return {"ok": True, "efi": efi_response}


@app.put("/boletos/{charge_id}/vencimento")
def update_boleto_due_date(charge_id: int, payload: UpdateBoletoDueDateRequest):
    if payload.charge_id != charge_id:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "message": "charge_id da URL deve ser igual ao do body.",
            },
        )
    efi_client = EfiClient(settings)
    try:
        efi_response = efi_client.update_charge_due_date(
            charge_id, payload.vencimento.isoformat()
        )
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)
    return {"ok": True, "efi": efi_response}


@app.put("/boletos/{charge_id}/metadata")
def update_boleto_metadata(charge_id: int, payload: UpdateBoletoMetadataRequest):
    if payload.charge_id != charge_id:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "message": "charge_id da URL deve ser igual ao do body.",
            },
        )
    efi_client = EfiClient(settings)
    try:
        efi_response = efi_client.update_charge_metadata(
            charge_id, payload.custom_id, payload.notification_url
        )
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)
    return {"ok": True, "efi": efi_response}


@app.get("/notificacoes/{notification_token}")
def get_notification(notification_token: str):
    efi_client = EfiClient(settings)
    try:
        efi_response = efi_client.get_notification(notification_token)
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)
    return {"ok": True, "efi": efi_response}


@app.post("/webhook")
def efi_webhook(payload: dict):
    logger.info("EFI webhook received payload=%s", sanitize_for_log(payload))
    return {"ok": True, "received": payload}
