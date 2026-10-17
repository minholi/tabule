from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, FoldedScalarString

from .config import DEFAULT_ATTRIBUTE_KEY, DEFAULT_CONTEXT, DEFAULT_REVIEW_LABEL, DEFAULT_THRESHOLD

CONTEXT_PLACEHOLDER = "Descreva o negócio e o tipo de atendimento para orientar a classificação."

SKELETON = """\
# Gerado por `tabule describe`. Revise o texto antes de usar o modo live.
# Mantenha os nomes das opções entre aspas e com o valor EXATO do atributo no Chatwoot.
# Descrição vazia impede a classificação até ser preenchida.
name: {name}
mode: shadow                 # shadow: só registra | live: grava no Chatwoot
inbox_ids: [{inbox_ids}]      # inboxes cujas conversas podem ser tabuladas
attribute_key: {attribute_key}
review_label: {review_label}
confidence_threshold: {threshold}    # calibrar com `tabule evaluate` antes do modo live
context: ""
options: {{}}
"""


class AccountFileError(Exception):
    pass


@dataclass(frozen=True)
class Pending:
    context: bool
    options: list[str]

    def __bool__(self) -> bool:
        return self.context or bool(self.options)


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 120
    return yaml


def _dump(data: Any) -> str:
    out = StringIO()
    _yaml().dump(data, out)
    return out.getvalue()


def skeleton(account_id: int, *, name: str | None, inbox_ids: list[int]) -> CommentedMap:
    text = SKELETON.format(
        name=json.dumps(name or str(account_id), ensure_ascii=False),
        inbox_ids=", ".join(str(i) for i in inbox_ids),
        attribute_key=DEFAULT_ATTRIBUTE_KEY,
        review_label=DEFAULT_REVIEW_LABEL,
        threshold=DEFAULT_THRESHOLD,
    )
    return _yaml().load(text)


def load(path: Path) -> CommentedMap | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = _yaml().load(text)
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise AccountFileError(f"{path} precisa ser um mapeamento YAML")
    return data


def _context_is_generic(value: Any) -> bool:
    text = str(value or "").strip()
    return text in ("", CONTEXT_PLACEHOLDER, DEFAULT_CONTEXT)


def _current_options(data: CommentedMap) -> CommentedMap:
    options = data.get("options")
    if options is None:
        return CommentedMap()
    if not isinstance(options, dict):
        raise AccountFileError("options deve ser um mapeamento: valor do Chatwoot -> descrição")
    return options


def pending(data: CommentedMap, remote_values: list[str], *, force: bool) -> Pending:
    if force:
        return Pending(True, list(remote_values))
    current = _current_options(data)
    empty = [value for value in remote_values if not str(current.get(value) or "").strip()]
    return Pending(_context_is_generic(data.get("context")), empty)


def apply(data: CommentedMap, context: str, descriptions: dict[str, str], todo: Pending) -> None:
    if todo.context:
        data["context"] = FoldedScalarString(context + "\n")
    if not todo.options:
        return
    options = data.get("options")
    if not isinstance(options, CommentedMap):
        options = CommentedMap()
        data["options"] = options
    for value in todo.options:
        options[DoubleQuotedScalarString(value) if value not in options else value] = descriptions[value]


def save(path: Path, data: CommentedMap) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_dump(data), encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return backup


def render(data: CommentedMap) -> str:
    return _dump(data)
