from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

OPENAI_BASE_URL = "https://api.openai.com/v1"

SYSTEM_PROMPT = (
    "Você ajuda a configurar um classificador de conversas de atendimento já encerradas. "
    "A partir do texto do operador e da lista de opções de tabulação, escreva:\n"
    "1. 'context': uma ou duas frases sobre o negócio e o tipo de atendimento, usando só o que o operador informou.\n"
    "2. Para cada opção, uma descrição curta (1 a 2 frases) dizendo QUANDO escolher aquela opção e em que ela "
    "difere das opções parecidas, de modo que as descrições sejam mutuamente exclusivas.\n"
    "Não invente fatos que não estejam no texto do operador nem sugeridos pelo nome da opção. "
    "Se não der para inferir o significado de uma opção só pelo nome, escreva a melhor hipótese e termine a "
    "descrição com ' (?)' para o operador revisar. "
    "Use exatamente os nomes das opções fornecidos como chaves, sem alterá-los."
)


class DescriberError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"OpenAI respondeu {status_code}: {detail}")
        self.status_code = status_code


@dataclass(frozen=True)
class Descriptions:
    context: str
    options: dict[str, str]


def _schema(options: list[str]) -> dict[str, Any]:
    return {
        "name": "descricoes",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["context", "options"],
            "properties": {
                "context": {"type": "string"},
                "options": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(options),
                    "properties": {value: {"type": "string"} for value in options},
                },
            },
        },
    }


class DescriptionWriter:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._http = httpx.Client(
            base_url=OPENAI_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def describe(self, *, about: str, options: list[str]) -> Descriptions:
        listing = "\n".join(f"- {value}" for value in options)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Texto do operador:\n{about}\n\nOpções de tabulação:\n{listing}"},
            ],
            "response_format": {"type": "json_schema", "json_schema": _schema(options)},
        }
        response = self._http.post("/chat/completions", json=body)
        if response.status_code >= 400:
            raise DescriberError(response.status_code, response.text[:300])
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DescriberError(response.status_code, "resposta sem choices") from exc
        if message.get("refusal"):
            raise DescriberError(response.status_code, f"modelo recusou: {message['refusal']}"[:300])
        try:
            data = json.loads(message.get("content") or "")
        except ValueError as exc:
            raise DescriberError(response.status_code, "conteúdo não é JSON") from exc
        return self._validate(response.status_code, data, options)

    @staticmethod
    def _validate(status: int, data: Any, options: list[str]) -> Descriptions:
        if not isinstance(data, dict):
            raise DescriberError(status, "JSON inesperado")
        context = data.get("context")
        described = data.get("options")
        if not isinstance(context, str) or not context.strip():
            raise DescriberError(status, "resposta sem context")
        if not isinstance(described, dict):
            raise DescriberError(status, "resposta sem options")
        missing = [value for value in options if value not in described]
        extra = [value for value in described if value not in options]
        if missing or extra:
            raise DescriberError(status, f"opções divergentes (faltam: {missing}; a mais: {extra})")
        texts = {value: str(described[value]).strip() for value in options}
        empty = [value for value, text in texts.items() if not text]
        if empty:
            raise DescriberError(status, f"descrição vazia para: {empty}")
        return Descriptions(context.strip(), texts)
