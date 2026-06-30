from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class Address(BaseModel):
    street: str
    number: str
    neighborhood: str
    zipcode: str
    city: str
    state: str
    complement: str | None = ""


class JuridicalPerson(BaseModel):
    corporate_name: str
    cnpj: str


class Customer(BaseModel):
    name: str | None = None
    cpf: str | None = None
    email: EmailStr
    phone_number: str
    birth: str | None = None
    address: Address | None = None
    juridical_person: JuridicalPerson | None = None

    @model_validator(mode="after")
    def validate_customer(self) -> "Customer":
        has_cpf = bool(self.cpf)
        has_company = self.juridical_person is not None

        if has_cpf == has_company:
            raise ValueError(
                "Envie exatamente um entre customer.cpf ou customer.juridical_person."
            )

        if has_cpf and not self.name:
            raise ValueError(
                "customer.name é obrigatório quando customer.cpf for informado."
            )

        return self


class BilletConfigurations(BaseModel):
    days_to_write_off: int | None = Field(default=None, ge=0, le=120)
    fine: int | None = Field(default=None, ge=0)
    interest: int | None = Field(default=None, ge=0)
    interest_type: str | None = None


class BilletDiscount(BaseModel):
    type: str
    value: int


class ConditionalDiscount(BaseModel):
    type: str
    value: int
    until_date: date


class CreateBoletoRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "empresa_id": "11111111-1111-1111-1111-111111111111",
                "tipo": "mensalidade",
                "valor": 150.75,
                "vencimento": "2026-04-10",
                "descricao": "Mensalidade abril/2026",
                "competencia_inicial": "2026-04-01",
                "competencia_final": "2026-04-30",
                "ano": "2026",
                "periodicidade": 1,
                "parcelas": 1,
                "descontos": 0,
                "percentual": 0,
                "base": 150.75,
                "item_name": "Mensalidade abril/2026",
                "item_amount": 1,
                "custom_id": "mensalidade-abril-2026-empresa-x",
                "message": "Boleto referente à mensalidade de abril/2026",
                "customer": {
                    "name": "Cliente Teste",
                    "cpf": "12345678909",
                    "email": "cliente.teste@example.com",
                    "phone_number": "11999999999",
                },
            }
        }
    )

    empresa_id: UUID | None = None
    tipo: str | None = None
    valor: Decimal = Field(..., gt=0, decimal_places=2)
    vencimento: date
    status: str | None = None
    competencia_inicial: date | None = None
    competencia_final: date | None = None
    faixa_id: UUID | None = None
    descricao: str | None = None
    ano: str | None = None
    periodicidade: int | None = None
    parcelas: int | None = None
    descontos: Decimal | None = Field(default=None, decimal_places=2)
    percentual: Decimal | None = Field(default=None, decimal_places=2)
    base: Decimal | None = Field(default=None, decimal_places=2)

    item_name: str = Field(default="Cobrança via boleto")
    item_amount: int = Field(default=1, ge=1)
    custom_id: str | None = None
    notification_url: str | None = None
    message: str | None = None
    customer: Customer
    configurations: BilletConfigurations | None = None
    discount: BilletDiscount | None = None
    conditional_discount: ConditionalDiscount | None = None


class CreateBoletoResponse(BaseModel):
    ok: bool
    efi: dict[str, Any]
    hasura: dict[str, Any] | None = None


class EfiChargeIdRequest(BaseModel):
    charge_id: int = Field(..., gt=0)


class UpdateBoletoDueDateRequest(EfiChargeIdRequest):
    vencimento: date


class UpdateBoletoMetadataRequest(EfiChargeIdRequest):
    custom_id: str | None = None
    notification_url: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "UpdateBoletoMetadataRequest":
        if not self.custom_id and not self.notification_url:
            raise ValueError("Informe custom_id e/ou notification_url.")
        return self


class ReconcileBoletosRequest(BaseModel):
    begin_date: date
    end_date: date
    status: str | None = None
    limit: int = Field(default=100, ge=1, le=500)
    page: int = Field(default=1, ge=1)
    apply: bool = False
    boleto_ids: list[UUID] | None = None
