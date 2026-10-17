from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .decisions import ChoiceAnswer


class OptionsMismatchError(Exception):
    pass


def build_choices(remote_values: list[str], descriptions: Mapping[str, str]) -> list[dict[str, str]]:
    if len(remote_values) < 2:
        raise OptionsMismatchError("o atributo precisa ter pelo menos 2 opções")
    missing = [value for value in remote_values if not descriptions.get(value, "").strip()]
    if missing:
        raise OptionsMismatchError("opções sem descrição no arquivo da conta: " + "; ".join(missing))
    return [{"value": value, "description": descriptions[value]} for value in remote_values]


def build_instructions(context: str) -> str:
    return (
        "Você classifica conversas de atendimento que já foram encerradas. "
        f"Contexto do atendimento: {context} "
        "Escolha a única opção que descreve melhor o desfecho da conversa, seguindo a descrição de cada opção. "
        "Baseie-se somente no conteúdo da transcrição. "
        "Mensagens marcadas como 'Modelo enviado' são abordagens automáticas da equipe; "
        "'Nota interna' são anotações feitas pelos atendentes. Não invente informações."
    )


@dataclass(frozen=True)
class Outcome:
    action: str
    choice: str | None
    confidence: float | None
    probabilities: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def decide(answer: ChoiceAnswer, *, valid_values: list[str], threshold: float) -> Outcome:
    if answer.refused:
        return Outcome("review", None, None, [], "modelo recusou a classificação")
    if answer.choice not in valid_values:
        return Outcome("review", answer.choice, answer.confidence, answer.probabilities, "opção fora da lista do Chatwoot")
    if answer.confidence is None or answer.confidence < threshold:
        return Outcome("review", answer.choice, answer.confidence, answer.probabilities, "confiança abaixo do limiar")
    return Outcome("accept", answer.choice, answer.confidence, answer.probabilities, "aceita")
