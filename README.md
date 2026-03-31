# EFI Boleto API (FastAPI)

API simples para:
1. criar um boleto na EFI via `POST /v1/charge/one-step`;
2. retornar o **body bruto** da EFI;
3. gravar o boleto na tabela `financeiro_boletos` no Hasura quando a criação for bem-sucedida.

A criação em um passo para boleto está documentada pela EFI na rota `/v1/charge/one-step`, e a resposta inclui campos como `charge_id`, `status`, `barcode`, `billet_link/link` e `pdf.charge`. A gravação no Hasura usa uma mutation `insert_<table>_one`, conforme a documentação oficial do Hasura. citeturn352660view0turn505322view1turn505322view2turn352660view2

## Estrutura

```bash
.
├── app
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   └── services
│       ├── efi.py
│       └── hasura.py
├── .env.example
├── .dockerignore
├── Dockerfile
├── README.md
└── requirements.txt
```

## Variáveis de ambiente

Copie o arquivo de exemplo:

```bash
cp .env.example .env
```

Preencha principalmente:

- `EFI_CLIENT_ID`
- `EFI_CLIENT_SECRET`
- `EFI_BASE_URL`
- `HASURA_GRAPHQL_URL`
- `HASURA_ADMIN_SECRET`

Para requests vindas do front-end (CORS), ajuste se necessário:

- `CORS_ALLOW_ORIGINS` (`*` ou lista separada por vírgula, ex.: `http://localhost:3000,https://app.seudominio.com`)
- `CORS_ALLOW_METHODS` (`*` ou lista separada por vírgula, ex.: `GET,POST,OPTIONS`)
- `CORS_ALLOW_HEADERS` (`*` ou lista separada por vírgula, ex.: `Authorization,Content-Type`)
- `CORS_ALLOW_CREDENTIALS` (`true`/`false`)

## Rodando localmente

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger:

- `http://localhost:8000/docs`

Healthcheck:

- `GET /health`

## Endpoint principal

### `POST /boletos`

### Body mínimo (CPF)

```json
{
  "valor": 150.75,
  "vencimento": "2026-04-10",
  "item_name": "Mensalidade abril/2026",
  "customer": {
    "name": "Cliente Teste",
    "cpf": "12345678909",
    "email": "cliente.teste@example.com",
    "phone_number": "11999999999"
  }
}
```

### Body com campos locais + EFI

```json
{
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
    "phone_number": "11999999999"
  }
}
```

### Body com empresa (CNPJ)

```json
{
  "valor": 499.90,
  "vencimento": "2026-04-10",
  "item_name": "Cobrança PJ",
  "customer": {
    "email": "financeiro@empresa.com",
    "phone_number": "11999999999",
    "juridical_person": {
      "corporate_name": "Empresa Exemplo LTDA",
      "cnpj": "99794567000144"
    }
  }
}
```

## O que precisa enviar no body

Obrigatórios:

- `valor`: decimal em reais, ex. `150.75`
- `vencimento`: data `YYYY-MM-DD`
- `customer.email`
- `customer.phone_number`
- **um destes dois formatos de cliente**:
  - PF: `customer.name` + `customer.cpf`
  - PJ: `customer.juridical_person.corporate_name` + `customer.juridical_person.cnpj`

Opcionais mais úteis:

- `empresa_id`
- `tipo`
- `descricao`
- `competencia_inicial`
- `competencia_final`
- `ano`
- `periodicidade`
- `parcelas`
- `descontos`
- `percentual`
- `base`
- `item_name`
- `item_amount`
- `custom_id`
- `message`
- `notification_url`
- `configurations`
- `discount`
- `conditional_discount`

## O que será retornado

### Sucesso

```json
{
  "ok": true,
  "efi": {
    "code": 200,
    "data": {
      "charge_id": 123456,
      "status": "waiting",
      "barcode": "...",
      "link": "...",
      "billet_link": "...",
      "pdf": {
        "charge": "..."
      }
    }
  },
  "hasura": {
    "data": {
      "insert_financeiro_boletos_one": {
        "id": "...",
        "efi_charge_id": "123456"
      }
    }
  }
}
```

### Erro na EFI

```json
{
  "ok": false,
  "stage": "efi",
  "status_code": 400,
  "efi": {
    "code": 400,
    "error": "..."
  }
}
```

### Erro no Hasura depois da EFI criar o boleto

```json
{
  "ok": false,
  "stage": "hasura",
  "status_code": 200,
  "efi": {
    "code": 200,
    "data": {
      "charge_id": 123456
    }
  },
  "hasura": {
    "errors": [
      {
        "message": "..."
      }
    ]
  }
}
```

## Como o insert local é montado

O projeto tenta preencher estes campos da sua tabela:

- `empresa_id`
- `tipo`
- `valor`
- `vencimento`
- `status` (somente se vier no body ou em `LOCAL_DEFAULT_STATUS`)
- `competencia_inicial`
- `competencia_final`
- `faixa_id`
- `descricao`
- `linha_digitavel` <- `data.barcode`
- `pdf_url` <- `data.pdf.charge` ou `data.billet_link/link`
- `efi_charge_id` <- `data.charge_id`
- `efi_status` <- `data.status`
- `efi_barcode` <- `data.barcode`
- `efi_pix_txid` <- `data.pix.txid` quando existir
- `ano`
- `periodicidade`
- `parcelas`
- `descontos`
- `percentual`
- `base`

Campos não enviados ficam de fora da mutation, então o Hasura/Postgres usa `NULL` ou o default da coluna.

## Exemplo curl

```bash
curl --request POST \
  --url http://localhost:8000/boletos \
  --header 'Content-Type: application/json' \
  --data '{
    "empresa_id": "11111111-1111-1111-1111-111111111111",
    "tipo": "mensalidade",
    "valor": 150.75,
    "vencimento": "2026-04-10",
    "descricao": "Mensalidade abril/2026",
    "ano": "2026",
    "item_name": "Mensalidade abril/2026",
    "customer": {
      "name": "Cliente Teste",
      "cpf": "12345678909",
      "email": "cliente.teste@example.com",
      "phone_number": "11999999999"
    }
  }'
```

## Docker

Build:

```bash
docker build -t efi-fastapi-boleto .
```

Run:

```bash
docker run --rm -p 8000:8000 --env-file .env efi-fastapi-boleto
```
