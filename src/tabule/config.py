from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_ACCOUNTS_DIR = Path("/etc/tabule/accounts")
DEFAULT_DB_PATH = Path("/var/lib/tabule/tabule.db")
DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_ATTRIBUTE_KEY = "tabulacao"
DEFAULT_REVIEW_LABEL = "tabulacao-revisar"
DEFAULT_CONTEXT = "Atendimento ao cliente."
DEFAULT_THRESHOLD = 0.8
VALID_MODES = ("shadow", "live")


class ConfigError(Exception):
    pass


def _required(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"{key} não definido")
    return value


def _optional(env: Mapping[str, str], key: str, default: str) -> str:
    return (env.get(key) or "").strip() or default


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} precisa ser um número inteiro") from exc


def parse_account_ids(raw: str) -> tuple[int, ...]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise ConfigError(f"TABULE_ALLOWED_ACCOUNT_IDS: '{part}' não é um id numérico") from exc
        if value <= 0:
            raise ConfigError(f"TABULE_ALLOWED_ACCOUNT_IDS: id inválido '{part}'")
        ids.add(value)
    if not ids:
        raise ConfigError("TABULE_ALLOWED_ACCOUNT_IDS está vazio")
    return tuple(sorted(ids))


@dataclass(frozen=True)
class Settings:
    allowed_account_ids: tuple[int, ...]
    chatwoot_base_url: str
    openai_api_key: str | None = None
    public_base_url: str | None = None
    openai_model: str = DEFAULT_MODEL
    openai_describe_model: str = DEFAULT_MODEL
    accounts_dir: Path = DEFAULT_ACCOUNTS_DIR
    db_path: Path = DEFAULT_DB_PATH
    timezone: str = DEFAULT_TIMEZONE
    host: str = "127.0.0.1"
    port: int = 8088
    resolution_delay_seconds: int = 60
    reconcile_interval_seconds: int = 900
    reconcile_pages: int = 4
    job_interval_seconds: float = 0.5
    max_attempts: int = 5
    max_transcript_chars: int = 40000
    http_timeout_seconds: float = 60.0
    webhook_max_body_bytes: int = 262144
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, require_openai: bool = True) -> Settings:
        env = os.environ if environ is None else environ
        openai_key = (env.get("TABULE_OPENAI_API_KEY") or "").strip() or None
        if require_openai and openai_key is None:
            raise ConfigError("TABULE_OPENAI_API_KEY não definido")
        public = (env.get("TABULE_PUBLIC_BASE_URL") or "").strip().rstrip("/") or None
        return cls(
            allowed_account_ids=parse_account_ids(_required(env, "TABULE_ALLOWED_ACCOUNT_IDS")),
            chatwoot_base_url=_required(env, "TABULE_CHATWOOT_BASE_URL").rstrip("/"),
            openai_api_key=openai_key,
            public_base_url=public,
            openai_model=_optional(env, "TABULE_OPENAI_MODEL", DEFAULT_MODEL),
            openai_describe_model=_optional(
                env, "TABULE_OPENAI_DESCRIBE_MODEL", _optional(env, "TABULE_OPENAI_MODEL", DEFAULT_MODEL)
            ),
            accounts_dir=Path(_optional(env, "TABULE_ACCOUNTS_DIR", str(DEFAULT_ACCOUNTS_DIR))),
            db_path=Path(_optional(env, "TABULE_DB_PATH", str(DEFAULT_DB_PATH))),
            timezone=_optional(env, "TABULE_TIMEZONE", DEFAULT_TIMEZONE),
            host=_optional(env, "TABULE_HOST", "127.0.0.1"),
            port=_int(env, "TABULE_PORT", 8088),
            resolution_delay_seconds=_int(env, "TABULE_RESOLUTION_DELAY_SECONDS", 60),
            reconcile_interval_seconds=_int(env, "TABULE_RECONCILE_INTERVAL_SECONDS", 900),
            reconcile_pages=_int(env, "TABULE_RECONCILE_PAGES", 4),
            max_transcript_chars=_int(env, "TABULE_MAX_TRANSCRIPT_CHARS", 40000),
            log_level=_optional(env, "TABULE_LOG_LEVEL", "INFO"),
        )


@dataclass(frozen=True)
class AccountConfig:
    id: int
    name: str
    mode: str
    inbox_ids: frozenset[int]
    attribute_key: str
    review_label: str
    confidence_threshold: float
    context: str
    options: Mapping[str, str]
    api_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)


def _read_account_file(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"arquivo ausente: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido em {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} precisa ser um mapeamento YAML")
    return data


def build_account(account_id: int, data: Mapping[str, Any], environ: Mapping[str, str]) -> AccountConfig:
    mode = str(data.get("mode") or "shadow").strip()
    if mode not in VALID_MODES:
        raise ConfigError(f"mode deve ser 'shadow' ou 'live' (valor: '{mode}')")
    inbox_ids = data.get("inbox_ids")
    if not isinstance(inbox_ids, list) or not inbox_ids or not all(type(i) is int and i > 0 for i in inbox_ids):
        raise ConfigError("inbox_ids deve ser uma lista não vazia de ids numéricos")
    raw_options = data.get("options")
    if not isinstance(raw_options, dict) or not raw_options:
        raise ConfigError("options deve ser um mapeamento não vazio: valor do Chatwoot -> descrição")
    options = {str(key): str(value or "").strip() for key, value in raw_options.items()}
    threshold = data.get("confidence_threshold", DEFAULT_THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise ConfigError("confidence_threshold deve ser um número entre 0 e 1")
    token = environ.get(f"TABULE_ACCOUNT_{account_id}_API_TOKEN", "").strip()
    secret = environ.get(f"TABULE_ACCOUNT_{account_id}_WEBHOOK_SECRET", "").strip()
    if not token:
        raise ConfigError(f"TABULE_ACCOUNT_{account_id}_API_TOKEN não definido")
    if not secret:
        raise ConfigError(f"TABULE_ACCOUNT_{account_id}_WEBHOOK_SECRET não definido")
    return AccountConfig(
        id=account_id,
        name=str(data.get("name") or account_id),
        mode=mode,
        inbox_ids=frozenset(inbox_ids),
        attribute_key=str(data.get("attribute_key") or DEFAULT_ATTRIBUTE_KEY).strip(),
        review_label=str(data.get("review_label") or DEFAULT_REVIEW_LABEL).strip(),
        confidence_threshold=float(threshold),
        context=str(data.get("context") or DEFAULT_CONTEXT).strip(),
        options=options,
        api_token=token,
        webhook_secret=secret,
    )


def load_accounts(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> tuple[dict[int, AccountConfig], dict[int, str]]:
    env = os.environ if environ is None else environ
    enabled: dict[int, AccountConfig] = {}
    errors: dict[int, str] = {}
    for account_id in settings.allowed_account_ids:
        try:
            data = _read_account_file(settings.accounts_dir / f"{account_id}.yaml")
            enabled[account_id] = build_account(account_id, data, env)
        except ConfigError as exc:
            errors[account_id] = str(exc)
    return enabled, errors
