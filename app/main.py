from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Request
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


def extract_notification_token(
    raw_body: bytes, content_type: str, query_params: dict[str, str]
) -> tuple[str | None, Any]:
    if query_params.get("notification"):
        return query_params["notification"], {
            "notification": query_params["notification"]
        }

    body_text = raw_body.decode("utf-8", errors="replace")
    if not body_text:
        return None, {}

    if "application/json" in content_type:
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            return None, body_text
        if isinstance(payload, dict):
            token = payload.get("notification") or payload.get("notification_token")
            return token, payload
        return None, payload

    parsed = parse_qs(body_text, keep_blank_values=True)
    if parsed:
        payload = {key: values[-1] if values else "" for key, values in parsed.items()}
        return payload.get("notification"), payload

    return None, body_text


def extract_boleto_status_updates(
    efi_notification_response: dict[str, Any],
) -> list[dict[str, str]]:
    data = efi_notification_response.get("data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        notifications = data.get("notifications") or data.get("items")
        items = notifications if isinstance(notifications, list) else [data]
    else:
        items = []

    updates: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        charge_id = pick_nested(
            item, ["identifiers.charge_id", "charge_id", "charge.id"]
        )
        status = pick_nested(item, ["status.current", "status", "charge.status"])
        if isinstance(status, dict):
            status = status.get("current") or status.get("status")
        if charge_id is None or status is None:
            logger.warning(
                "EFI notification item without charge_id/status item=%s",
                sanitize_for_log(item),
            )
            continue
        updates.append({"charge_id": str(charge_id), "efi_status": str(status)})
    return updates


def pick_nested(data: dict[str, Any], paths: list[str]) -> Any:
    for path in paths:
        current: Any = data
        found = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    return None


def process_efi_notification(notification_token: str) -> None:
    efi_client = EfiClient(settings)
    hasura_client = HasuraClient(settings)
    try:
        efi_response = efi_client.get_notification(notification_token)
    except EfiAPIError as exc:
        logger.error(
            "EFI webhook notification lookup failed token=%s status_code=%s body=%s",
            notification_token,
            exc.status_code,
            sanitize_for_log(exc.body),
        )
        return

    logger.info(
        "EFI webhook notification lookup succeeded token=%s response=%s",
        notification_token,
        sanitize_for_log(efi_response),
    )

    updates = extract_boleto_status_updates(efi_response)
    if not updates:
        logger.warning(
            "EFI webhook produced no boleto status updates token=%s response=%s",
            notification_token,
            sanitize_for_log(efi_response),
        )
        return

    for update in updates:
        try:
            hasura_response = hasura_client.update_financeiro_boleto_efi_status(
                update["charge_id"],
                update["efi_status"],
            )
        except HasuraAPIError as exc:
            logger.error(
                "Hasura boleto status update failed token=%s charge_id=%s efi_status=%s status_code=%s body=%s",
                notification_token,
                update["charge_id"],
                update["efi_status"],
                exc.status_code,
                sanitize_for_log(exc.body),
            )
            continue

        logger.info(
            "Hasura boleto status update completed token=%s charge_id=%s efi_status=%s response=%s",
            notification_token,
            update["charge_id"],
            update["efi_status"],
            sanitize_for_log(hasura_response),
        )


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
async def efi_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    notification_token, payload = extract_notification_token(
        raw_body,
        request.headers.get("content-type", ""),
        dict(request.query_params),
    )
    logger.info(
        "EFI webhook received content_type=%s token_present=%s payload=%s",
        request.headers.get("content-type", ""),
        bool(notification_token),
        sanitize_for_log(payload),
    )

    if notification_token:
        background_tasks.add_task(process_efi_notification, notification_token)

    return {"ok": True, "notification": notification_token}
