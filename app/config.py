from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "EFI Boleto API"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True
    cors_allow_origins: str = Field(default="*", alias="CORS_ALLOW_ORIGINS")
    cors_allow_methods: str = Field(default="*", alias="CORS_ALLOW_METHODS")
    cors_allow_headers: str = Field(default="*", alias="CORS_ALLOW_HEADERS")
    cors_allow_credentials: bool = Field(default=False, alias="CORS_ALLOW_CREDENTIALS")

    efi_client_id: str = Field(default="", alias="EFI_CLIENT_ID")
    efi_client_secret: str = Field(default="", alias="EFI_CLIENT_SECRET")
    efi_sandbox: bool = Field(default=True, alias="EFI_SANDBOX")
    efi_base_url: str = Field(
        default="https://cobrancas-h.api.efipay.com.br",
        alias="EFI_BASE_URL",
    )
    efi_timeout_seconds: int = Field(default=30, alias="EFI_TIMEOUT_SECONDS")
    efi_webhook_url: str | None = Field(default=None, alias="EFI_WEBHOOK_URL")

    hasura_graphql_url: str = Field(default="", alias="HASURA_GRAPHQL_URL")
    hasura_admin_secret: str = Field(default="", alias="HASURA_ADMIN_SECRET")
    hasura_timeout_seconds: int = Field(default=20, alias="HASURA_TIMEOUT_SECONDS")

    local_default_tipo: str = Field(default="boleto", alias="LOCAL_DEFAULT_TIPO")
    local_default_status: str | None = Field(default=None, alias="LOCAL_DEFAULT_STATUS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.efi_base_url:
        settings.efi_base_url = (
            "https://cobrancas-h.api.efipay.com.br"
            if settings.efi_sandbox
            else "https://cobrancas.api.efipay.com.br"
        )
    return settings


def parse_cors_values(raw_value: str) -> list[str]:
    value = raw_value.strip()
    if value == "*":
        return ["*"]
    return [item.strip() for item in value.split(",") if item.strip()]
