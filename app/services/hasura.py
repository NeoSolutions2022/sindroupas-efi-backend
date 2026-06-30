from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

import requests
from requests import RequestException

from app.config import Settings
from app.logging_utils import sanitize_for_log
from app.models import CreateBoletoRequest

logger = logging.getLogger(__name__)


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


UPDATE_EFI_STATUS_MUTATION = """
mutation UpdateFinanceiroBoletoEfiStatus($efi_charge_id: String!, $_set: financeiro_boletos_set_input!) {
  update_financeiro_boletos(where: {efi_charge_id: {_eq: $efi_charge_id}}, _set: $_set) {
    affected_rows
    returning {
      id
      efi_charge_id
      efi_status
      updated_at
    }
  }
}
"""

LIST_BOLETOS_FOR_RECONCILIATION_QUERY = """
query ListBoletosForReconciliation($begin_date: date!, $end_date: date!) {
  financeiro_boletos(where: {vencimento: {_gte: $begin_date, _lte: $end_date}}) {
    id
    empresa_id
    valor
    vencimento
    status
    descricao
    linha_digitavel
    pdf_url
    efi_charge_id
    efi_status
    efi_barcode
    efi_pix_txid
    updated_at
  }
}
"""

UPDATE_BOLETO_EFI_DATA_MUTATION = """
mutation UpdateBoletoEfiData($id: uuid!, $_set: financeiro_boletos_set_input!) {
  update_financeiro_boletos_by_pk(pk_columns: {id: $id}, _set: $_set) {
    id
    efi_charge_id
    efi_status
    efi_barcode
    linha_digitavel
    pdf_url
    efi_pix_txid
    updated_at
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
        insert_object = self._build_insert_object(payload, efi_response)
        logger.info(
            "Hasura boleto insert request empresa_id=%s efi_charge_id=%s",
            insert_object.get("empresa_id"),
            insert_object.get("efi_charge_id"),
        )
        return self._post_graphql(
            INSERT_MUTATION,
            {"object": insert_object},
            "Hasura boleto insert",
            insert_object,
        )

    def update_financeiro_boleto_efi_status(
        self,
        charge_id: str | int,
        efi_status: str,
    ) -> dict[str, Any]:
        efi_charge_id = str(charge_id)
        update_object = {"efi_status": efi_status}
        logger.info(
            "Hasura boleto status update request efi_charge_id=%s efi_status=%s",
            efi_charge_id,
            efi_status,
        )
        body = self._post_graphql(
            UPDATE_EFI_STATUS_MUTATION,
            {
                "efi_charge_id": efi_charge_id,
                "_set": update_object,
            },
            "Hasura boleto status update",
        )
        affected_rows = (
            (body.get("data") or {})
            .get("update_financeiro_boletos", {})
            .get(
                "affected_rows",
                0,
            )
        )
        returning = (
            (body.get("data") or {})
            .get("update_financeiro_boletos", {})
            .get("returning", [])
        )
        if affected_rows == 0:
            logger.warning(
                "Hasura boleto status update matched no rows efi_charge_id=%s efi_status=%s",
                efi_charge_id,
                efi_status,
            )
        else:
            logger.info(
                "Hasura boleto status update affected rows efi_charge_id=%s efi_status=%s affected_rows=%s returning=%s",
                efi_charge_id,
                efi_status,
                affected_rows,
                sanitize_for_log(returning),
            )
        return body

    def list_boletos_for_reconciliation(
        self,
        begin_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        body = self._post_graphql(
            LIST_BOLETOS_FOR_RECONCILIATION_QUERY,
            {"begin_date": begin_date, "end_date": end_date},
            "Hasura boleto reconciliation list",
        )
        return (body.get("data") or {}).get("financeiro_boletos", [])

    def update_boleto_efi_data(
        self,
        boleto_id: str,
        efi_data: dict[str, Any],
    ) -> dict[str, Any]:
        update_object = {k: v for k, v in efi_data.items() if v is not None}
        logger.info(
            "Hasura boleto reconciliation update request boleto_id=%s efi_charge_id=%s",
            boleto_id,
            update_object.get("efi_charge_id"),
        )
        return self._post_graphql(
            UPDATE_BOLETO_EFI_DATA_MUTATION,
            {"id": boleto_id, "_set": update_object},
            "Hasura boleto reconciliation update",
            update_object,
        )

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
            "competencia_inicial": (
                payload.competencia_inicial.isoformat()
                if payload.competencia_inicial
                else None
            ),
            "competencia_final": (
                payload.competencia_final.isoformat()
                if payload.competencia_final
                else None
            ),
            "faixa_id": str(payload.faixa_id) if payload.faixa_id else None,
            "descricao": payload.descricao or payload.item_name,
            "linha_digitavel": self._pick_first(data, ["barcode"]),
            "pdf_url": self._pick_first(data, ["pdf.charge", "billet_link", "link"]),
            "efi_charge_id": self._string_or_none(
                self._pick_first(data, ["charge_id"])
            ),
            "efi_status": self._string_or_none(self._pick_first(data, ["status"])),
            "efi_barcode": self._pick_first(data, ["barcode"]),
            "efi_pix_txid": self._pick_first(data, ["pix.txid"]),
            "ano": payload.ano,
            "periodicidade": payload.periodicidade,
            "parcelas": payload.parcelas,
            "descontos": (
                float(payload.descontos) if payload.descontos is not None else None
            ),
            "percentual": (
                float(payload.percentual) if payload.percentual is not None else None
            ),
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

    def _post_graphql(
        self,
        query: str,
        variables: dict[str, Any],
        operation_name: str,
        log_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "x-hasura-admin-secret": self.settings.hasura_admin_secret,
        }
        started_at = perf_counter()
        try:
            response = requests.post(
                self.settings.hasura_graphql_url,
                headers=headers,
                json={
                    "query": query,
                    "variables": variables,
                },
                timeout=self.settings.hasura_timeout_seconds,
            )
        except RequestException as exc:
            elapsed_ms = self._elapsed_ms(started_at)
            logger.exception(
                "%s connection failed elapsed_ms=%s context=%s",
                operation_name,
                elapsed_ms,
                sanitize_for_log(log_context or variables),
            )
            raise HasuraAPIError(
                502,
                {
                    "message": "Falha de conexão ao executar operação no Hasura.",
                    "operation": operation_name,
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            ) from exc

        elapsed_ms = self._elapsed_ms(started_at)
        body = self._safe_json(response)
        if not response.ok:
            logger.error(
                "%s failed status_code=%s elapsed_ms=%s body=%s",
                operation_name,
                response.status_code,
                elapsed_ms,
                sanitize_for_log(body),
            )
            raise HasuraAPIError(response.status_code, body)
        if body.get("errors"):
            logger.error(
                "%s GraphQL errors elapsed_ms=%s body=%s",
                operation_name,
                elapsed_ms,
                sanitize_for_log(body),
            )
            raise HasuraAPIError(422, body)

        logger.info(
            "%s succeeded status_code=%s elapsed_ms=%s",
            operation_name,
            response.status_code,
            elapsed_ms,
        )
        return body

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round((perf_counter() - started_at) * 1000)
