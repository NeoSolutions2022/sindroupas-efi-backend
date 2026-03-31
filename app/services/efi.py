from __future__ import annotations

import base64
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import requests

from app.config import Settings
from app.models import CreateBoletoRequest


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
        response = requests.post(
            f"{self.settings.efi_base_url}/v1/authorize",
            headers=headers,
            json={"grant_type": "client_credentials"},
            timeout=self.settings.efi_timeout_seconds,
        )

        if not response.ok:
            raise EfiAPIError(response.status_code, self._safe_json(response))

        return response.json()["access_token"]

    def create_boleto(self, payload: CreateBoletoRequest) -> dict[str, Any]:
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        body = self._build_efi_body(payload)
        response = requests.post(
            f"{self.settings.efi_base_url}/v1/charge/one-step",
            headers=headers,
            json=body,
            timeout=self.settings.efi_timeout_seconds,
        )

        if not response.ok:
            raise EfiAPIError(response.status_code, self._safe_json(response))

        return response.json()

    def _build_efi_body(self, payload: CreateBoletoRequest) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if payload.custom_id:
            metadata["custom_id"] = payload.custom_id
        if payload.notification_url:
            metadata["notification_url"] = payload.notification_url

        banking_billet: dict[str, Any] = {
            "expire_at": payload.vencimento.isoformat(),
            "customer": payload.customer.model_dump(exclude_none=True),
        }

        if payload.message:
            banking_billet["message"] = payload.message
        if payload.configurations:
            banking_billet["configurations"] = payload.configurations.model_dump(exclude_none=True)
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
