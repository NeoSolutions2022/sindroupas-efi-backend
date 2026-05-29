from __future__ import annotations

import base64
import logging
from decimal import Decimal, ROUND_HALF_UP
from time import perf_counter
from typing import Any

import requests
from requests import RequestException

from app.config import Settings
from app.logging_utils import sanitize_for_log
from app.models import CreateBoletoRequest

logger = logging.getLogger(__name__)


class EfiAPIError(Exception):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"EFI request failed with status {status_code}")


class EfiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _basic_auth_header(self) -> str:
        raw = f"{self.settings.efi_client_id}:{self.settings.efi_client_secret}"
        return base64.b64encode(raw.encode()).decode()

    def get_access_token(self) -> str:
        headers = {
            "Authorization": f"Basic {self._basic_auth_header()}",
            "Content-Type": "application/json",
        }
        started_at = perf_counter()
        try:
            response = requests.post(
                f"{self.settings.efi_base_url}/v1/authorize",
                headers=headers,
                json={"grant_type": "client_credentials"},
                timeout=self.settings.efi_timeout_seconds,
            )
        except RequestException as exc:
            elapsed_ms = self._elapsed_ms(started_at)
            logger.exception("EFI auth connection failed elapsed_ms=%s", elapsed_ms)
            raise EfiAPIError(
                502,
                {
                    "message": "Falha de conexão ao autenticar na EFI.",
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            ) from exc

        elapsed_ms = self._elapsed_ms(started_at)
        if not response.ok:
            body = self._safe_json(response)
            logger.error(
                "EFI auth failed status_code=%s elapsed_ms=%s body=%s",
                response.status_code,
                elapsed_ms,
                sanitize_for_log(body),
            )
            raise EfiAPIError(response.status_code, body)

        logger.info(
            "EFI auth succeeded status_code=%s elapsed_ms=%s",
            response.status_code,
            elapsed_ms,
        )
        return response.json()["access_token"]

    def create_boleto(self, payload: CreateBoletoRequest) -> dict[str, Any]:
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        body = self._build_efi_body(payload)
        logger.info(
            "EFI create boleto request custom_id=%s has_notification_url=%s",
            payload.custom_id,
            bool((body.get("metadata") or {}).get("notification_url")),
        )
        started_at = perf_counter()
        try:
            response = requests.post(
                f"{self.settings.efi_base_url}/v1/charge/one-step",
                headers=headers,
                json=body,
                timeout=self.settings.efi_timeout_seconds,
            )
        except RequestException as exc:
            elapsed_ms = self._elapsed_ms(started_at)
            logger.exception(
                "EFI create boleto connection failed elapsed_ms=%s request_body=%s",
                elapsed_ms,
                sanitize_for_log(body),
            )
            raise EfiAPIError(
                502,
                {
                    "message": "Falha de conexão ao criar boleto na EFI.",
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            ) from exc

        elapsed_ms = self._elapsed_ms(started_at)
        response_body = self._safe_json(response)
        if not response.ok:
            logger.error(
                "EFI create boleto failed status_code=%s elapsed_ms=%s request_body=%s response_body=%s",
                response.status_code,
                elapsed_ms,
                sanitize_for_log(body),
                sanitize_for_log(response_body),
            )
            raise EfiAPIError(response.status_code, response_body)

        logger.info(
            "EFI create boleto succeeded status_code=%s elapsed_ms=%s charge_id=%s efi_status=%s",
            response.status_code,
            elapsed_ms,
            (response_body.get("data") or {}).get("charge_id"),
            (response_body.get("data") or {}).get("status"),
        )
        return response_body

    def get_charge(self, charge_id: int) -> dict[str, Any]:
        return self._request("get", f"/v1/charge/{charge_id}")

    def cancel_charge(self, charge_id: int) -> dict[str, Any]:
        return self._request("put", f"/v1/charge/{charge_id}/cancel")

    def update_charge_due_date(self, charge_id: int, vencimento: str) -> dict[str, Any]:
        return self._request(
            "put", f"/v1/charge/{charge_id}/billet", {"expire_at": vencimento}
        )

    def update_charge_metadata(
        self,
        charge_id: int,
        custom_id: str | None = None,
        notification_url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if custom_id:
            body["custom_id"] = custom_id
        if notification_url:
            body["notification_url"] = notification_url
        return self._request("put", f"/v1/charge/{charge_id}/metadata", body)

    def get_notification(self, notification_token: str) -> dict[str, Any]:
        return self._request("get", f"/v1/notification/{notification_token}")

    def _build_efi_body(self, payload: CreateBoletoRequest) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if payload.custom_id:
            metadata["custom_id"] = payload.custom_id
        metadata["notification_url"] = (
            payload.notification_url or self.settings.efi_webhook_url
        )

        banking_billet: dict[str, Any] = {
            "expire_at": payload.vencimento.isoformat(),
            "customer": payload.customer.model_dump(exclude_none=True),
        }

        if payload.message:
            banking_billet["message"] = payload.message
        if payload.configurations:
            banking_billet["configurations"] = payload.configurations.model_dump(
                exclude_none=True
            )
        if payload.discount:
            banking_billet["discount"] = payload.discount.model_dump(exclude_none=True)
        if payload.conditional_discount:
            data = payload.conditional_discount.model_dump(exclude_none=True)
            data["until_date"] = payload.conditional_discount.until_date.isoformat()
            banking_billet["conditional_discount"] = data

        body: dict[str, Any] = {
            "items": [
                {
                    "name": payload.item_name,
                    "value": self._to_cents(payload.valor),
                    "amount": payload.item_amount,
                }
            ],
            "payment": {
                "banking_billet": banking_billet,
            },
        }

        metadata = {k: v for k, v in metadata.items() if v}
        if metadata:
            body["metadata"] = metadata

        return body

    @staticmethod
    def _to_cents(value: Decimal) -> int:
        cents = (value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(cents)

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        started_at = perf_counter()
        try:
            response = requests.request(
                method.upper(),
                f"{self.settings.efi_base_url}{path}",
                headers=headers,
                json=body,
                timeout=self.settings.efi_timeout_seconds,
            )
        except RequestException as exc:
            elapsed_ms = self._elapsed_ms(started_at)
            logger.exception(
                "EFI request connection failed method=%s path=%s elapsed_ms=%s request_body=%s",
                method.upper(),
                path,
                elapsed_ms,
                sanitize_for_log(body),
            )
            raise EfiAPIError(
                502,
                {
                    "message": "Falha de conexão ao chamar endpoint da EFI.",
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            ) from exc

        elapsed_ms = self._elapsed_ms(started_at)
        response_body = self._safe_json(response)
        if not response.ok:
            logger.error(
                "EFI request failed method=%s path=%s status_code=%s elapsed_ms=%s response_body=%s",
                method.upper(),
                path,
                response.status_code,
                elapsed_ms,
                sanitize_for_log(response_body),
            )
            raise EfiAPIError(response.status_code, response_body)

        logger.info(
            "EFI request succeeded method=%s path=%s status_code=%s elapsed_ms=%s",
            method.upper(),
            path,
            response.status_code,
            elapsed_ms,
        )
        return response_body

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round((perf_counter() - started_at) * 1000)
