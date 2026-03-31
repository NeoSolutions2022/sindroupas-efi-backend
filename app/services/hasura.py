from __future__ import annotations

from typing import Any

import requests
from requests import RequestException

from app.config import Settings
from app.models import CreateBoletoRequest


class HasuraAPIError(Exception):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Hasura request failed with status {status_code}")


INSERT_MUTATION = """
mutation InsertFinanceiroBoleto($object: financeiro_boletos_insert_input!) {
  insert_financeiro_boletos_one(object: $object) {
    id
    empresa_id
    tipo
    valor
    vencimento
    status
    competencia_inicial
    competencia_final
    faixa_id
    descricao
    linha_digitavel
    pdf_url
    efi_charge_id
    efi_status
    efi_barcode
    efi_pix_txid
    created_at
    updated_at
    ano
    periodicidade
    parcelas
    descontos
    percentual
    base
  }
}
"""


class HasuraClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def insert_financeiro_boleto(
        self,
        payload: CreateBoletoRequest,
        efi_response: dict[str, Any],
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "x-hasura-admin-secret": self.settings.hasura_admin_secret,
        }
        try:
            response = requests.post(
                self.settings.hasura_graphql_url,
                headers=headers,
                json={
                    "query": INSERT_MUTATION,
                    "variables": {
                        "object": self._build_insert_object(payload, efi_response),
                    },
                },
                timeout=self.settings.hasura_timeout_seconds,
            )
        except RequestException as exc:
            raise HasuraAPIError(
                502,
                {
                    "message": "Falha de conexão ao inserir boleto no Hasura.",
                    "error": str(exc),
                },
            ) from exc

        body = self._safe_json(response)
        if not response.ok:
            raise HasuraAPIError(response.status_code, body)
        if body.get("errors"):
            raise HasuraAPIError(422, body)

        return body

    def _build_insert_object(
        self,
        payload: CreateBoletoRequest,
        efi_response: dict[str, Any],
    ) -> dict[str, Any]:
        data = efi_response.get("data") or {}
        obj: dict[str, Any] = {
            "empresa_id": str(payload.empresa_id) if payload.empresa_id else None,
            "tipo": payload.tipo or self.settings.local_default_tipo,
            "valor": float(payload.valor),
            "vencimento": payload.vencimento.isoformat(),
            "status": payload.status or self.settings.local_default_status,
            "competencia_inicial": payload.competencia_inicial.isoformat() if payload.competencia_inicial else None,
            "competencia_final": payload.competencia_final.isoformat() if payload.competencia_final else None,
            "faixa_id": str(payload.faixa_id) if payload.faixa_id else None,
            "descricao": payload.descricao or payload.item_name,
            "linha_digitavel": self._pick_first(data, ["barcode"]),
            "pdf_url": self._pick_first(data, ["pdf.charge", "billet_link", "link"]),
            "efi_charge_id": self._string_or_none(self._pick_first(data, ["charge_id"])),
            "efi_status": self._string_or_none(self._pick_first(data, ["status"])),
            "efi_barcode": self._pick_first(data, ["barcode"]),
            "efi_pix_txid": self._pick_first(data, ["pix.txid"]),
            "ano": payload.ano,
            "periodicidade": payload.periodicidade,
            "parcelas": payload.parcelas,
            "descontos": float(payload.descontos) if payload.descontos is not None else None,
            "percentual": float(payload.percentual) if payload.percentual is not None else None,
            "base": float(payload.base) if payload.base is not None else None,
        }
        return {k: v for k, v in obj.items() if v is not None}

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _pick_first(data: dict[str, Any], paths: list[str]) -> Any:
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

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}
