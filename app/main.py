from __future__ import annotations

import json
import logging
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings, parse_cors_values
from app.logging_utils import sanitize_for_log
from app.models import (
    CreateBoletoRequest,
    CreateBoletoResponse,
    ReconcileBoletosRequest,
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


def cents_from_value(value: Any) -> int | None:
    if value is None:
        return None
    return int(
        (Decimal(str(value)) * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def efi_charge_to_update_object(charge: dict[str, Any]) -> dict[str, Any]:
    payment = charge.get("payment") if isinstance(charge.get("payment"), dict) else {}
    billet = (
        payment.get("banking_billet")
        if isinstance(payment.get("banking_billet"), dict)
        else {}
    )
    pdf = billet.get("pdf") if isinstance(billet.get("pdf"), dict) else {}
    return {
        "efi_charge_id": str(charge.get("id") or charge.get("charge_id")),
        "efi_status": (
            str(charge.get("status")) if charge.get("status") is not None else None
        ),
        "efi_barcode": billet.get("barcode") or charge.get("barcode"),
        "linha_digitavel": billet.get("barcode") or charge.get("barcode"),
        "pdf_url": pdf.get("charge") or billet.get("billet_link") or billet.get("link"),
        "efi_pix_txid": pick_nested(charge, ["pix.txid", "payment.pix.txid"]),
    }


def reconcile_boletos(payload: ReconcileBoletosRequest) -> dict[str, Any]:
    efi_client = EfiClient(settings)
    hasura_client = HasuraClient(settings)
    efi_response = efi_client.list_charges(
        payload.begin_date.isoformat(),
        payload.end_date.isoformat(),
        payload.status,
        payload.limit,
        payload.page,
    )
    efi_charges = (
        efi_response.get("data") if isinstance(efi_response.get("data"), list) else []
    )
    hasura_boletos = hasura_client.list_boletos_for_reconciliation(
        payload.begin_date.isoformat(),
        payload.end_date.isoformat(),
    )
    hasura_charge_ids = {
        str(b.get("efi_charge_id")) for b in hasura_boletos if b.get("efi_charge_id")
    }

    efi_by_key: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(list)
    for charge in efi_charges:
        billet = pick_nested(charge, ["payment.banking_billet"]) or {}
        due_date = billet.get("expire_at") or charge.get("expire_at")
        efi_by_key[
            (
                str(due_date),
                cents_from_value(Decimal(str(charge.get("total", 0))) / 100),
            )
        ].append(charge)

    hasura_by_key: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(
        list
    )
    for boleto in hasura_boletos:
        hasura_by_key[
            (str(boleto.get("vencimento")), cents_from_value(boleto.get("valor")))
        ].append(boleto)

    matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for key, boletos in hasura_by_key.items():
        charges = efi_by_key.get(key, [])
        if len(boletos) != 1 or len(charges) != 1:
            if charges:
                conflicts.append(
                    {
                        "key": key,
                        "hasura_count": len(boletos),
                        "efi_count": len(charges),
                    }
                )
            continue
        boleto = boletos[0]
        charge = charges[0]
        charge_id = str(charge.get("id") or charge.get("charge_id"))
        if boleto.get("efi_charge_id") == charge_id:
            continue
        if charge_id in hasura_charge_ids:
            conflicts.append(
                {
                    "key": key,
                    "boleto_id": boleto.get("id"),
                    "efi_charge_id": charge_id,
                    "reason": "charge_id_already_used",
                }
            )
            continue
        if payload.boleto_ids and str(boleto.get("id")) not in {
            str(item) for item in payload.boleto_ids
        }:
            continue
        matches.append(
            {
                "boleto": boleto,
                "efi_charge": charge,
                "update": efi_charge_to_update_object(charge),
            }
        )

    applied: list[dict[str, Any]] = []
    if payload.apply:
        for match in matches:
            result = hasura_client.update_boleto_efi_data(
                match["boleto"]["id"], match["update"]
            )
            applied.append({"boleto_id": match["boleto"]["id"], "hasura": result})

    return {
        "ok": True,
        "applied": payload.apply,
        "summary": {
            "efi_charges": len(efi_charges),
            "hasura_boletos": len(hasura_boletos),
            "safe_matches": len(matches),
            "conflicts": len(conflicts),
            "updated": len(applied),
        },
        "matches": matches,
        "conflicts": conflicts,
        "applied_results": applied,
        "efi": efi_response,
    }


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
    logger.info(
        "EFI webhook extracted boleto status updates token=%s updates=%s",
        notification_token,
        sanitize_for_log(updates),
    )
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

        update_result = (hasura_response.get("data") or {}).get(
            "update_financeiro_boletos", {}
        )
        logger.info(
            "Hasura boleto status update completed token=%s charge_id=%s efi_status=%s affected_rows=%s returning=%s",
            notification_token,
            update["charge_id"],
            update["efi_status"],
            update_result.get("affected_rows"),
            sanitize_for_log(update_result.get("returning")),
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


@app.get("/reconciliacao/boletos", response_class=HTMLResponse)
def reconciliation_page():
    return """
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <title>Reconciliação de boletos EFI x Hasura</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; }
    label { display: block; margin: .75rem 0; }
    input, button { padding: .5rem; }
    pre { background: #111827; color: #e5e7eb; padding: 1rem; overflow: auto; }
    .danger { color: #991b1b; font-weight: 700; }
  </style>
</head>
<body>
  <h1>Reconciliação de boletos EFI x Hasura</h1>
  <p>Primeiro execute uma prévia. A atualização só grava no Hasura quando <code>apply=true</code>.</p>
  <form id="form">
    <label>Início <input name="begin_date" type="date" required></label>
    <label>Fim <input name="end_date" type="date" required></label>
    <label>Status EFI opcional <input name="status" placeholder="waiting, paid..."></label>
    <label>Limite EFI <input name="limit" type="number" min="1" max="500" value="100"></label>
    <label>Página EFI <input name="page" type="number" min="1" value="1"></label>
    <button type="submit">Consultar prévia segura</button>
    <button type="button" id="apply" class="danger">Aplicar matches seguros</button>
  </form>
  <pre id="out">Aguardando consulta...</pre>
  <script>
    async function run(apply) {
      const form = new FormData(document.getElementById('form'));
      const payload = Object.fromEntries(form.entries());
      payload.limit = Number(payload.limit || 100);
      payload.page = Number(payload.page || 1);
      payload.apply = apply;
      if (!payload.status) delete payload.status;
      const res = await fetch('/reconciliacao/boletos', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payload)
      });
      document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
    }
    document.getElementById('form').addEventListener('submit', (event) => {
      event.preventDefault();
      run(false);
    });
    document.getElementById('apply').addEventListener('click', () => {
      if (confirm('Aplicar somente os matches seguros no Hasura?')) run(true);
    });
  </script>
</body>
</html>
"""


@app.post("/reconciliacao/boletos")
def reconcile_boletos_endpoint(payload: ReconcileBoletosRequest):
    try:
        return reconcile_boletos(payload)
    except EfiAPIError as exc:
        return downstream_error_response("efi", exc)
    except HasuraAPIError as exc:
        return downstream_error_response("hasura", exc)


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
