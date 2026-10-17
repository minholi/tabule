from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

QUESTION_NAME = "tabulacao"
OPENAI_BASE_URL = "https://api.openai.com/v1"


class DecisionsError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"OpenAI Decisions respondeu {status_code}: {detail}")
        self.status_code = status_code


@dataclass(frozen=True)
class ChoiceAnswer:
    refused: bool
    choice: str | None
    confidence: float | None
    probabilities: list[dict[str, Any]] = field(default_factory=list)


class DecisionsClient:
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

    def choose(self, *, transcript: str, instructions: str, choices: list[dict[str, str]]) -> ChoiceAnswer:
        body = {
            "model": self.model,
            "input": transcript,
            "questions": [
                {"type": "choice", "name": QUESTION_NAME, "instructions": instructions, "choices": choices},
            ],
        }
        response = self._http.post("/decisions", json=body)
        if response.status_code >= 400:
            raise DecisionsError(response.status_code, response.text[:300])
        answers = response.json().get("answers") or []
        if not answers:
            raise DecisionsError(response.status_code, "resposta sem answers")
        answer = answers[0]
        if answer.get("type") == "refusal":
            return ChoiceAnswer(refused=True, choice=None, confidence=None)
        if answer.get("type") != "choice":
            raise DecisionsError(response.status_code, f"tipo de resposta inesperado: {answer.get('type')}")
        confidence = answer.get("confidence")
        return ChoiceAnswer(
            refused=False,
            choice=answer.get("choice"),
            confidence=float(confidence) if confidence is not None else None,
            probabilities=answer.get("probabilities") or [],
        )
