from __future__ import annotations

import argparse
import hmac
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Mapping, NamedTuple, Sequence

import httpx
import uvicorn

from . import accountfile
from .app import build_pipeline, create_app
from .chatwoot import WEBHOOK_EVENT, ChatwootClient, ChatwootError, RemoteConfigError, attribute_options
from .classifier import build_choices, build_instructions
from .config import AccountConfig, ConfigError, Settings, load_accounts
from .decisions import DecisionsClient
from .describer import DescriberError, DescriptionWriter
from .store import Store
from .transcript import build_transcript

THRESHOLDS = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)


class EvalRow(NamedTuple):
    display_id: int
    human: str
    choice: str | None
    confidence: float | None


def _human_value(conversation: Mapping[str, Any], key: str) -> Any:
    return (conversation.get("custom_attributes") or {}).get(key)


def _require_account(accounts: Mapping[int, AccountConfig], account_id: int) -> AccountConfig:
    account = accounts.get(account_id)
    if account is None:
        raise SystemExit(f"A conta {account_id} não está habilitada (veja TABULE_ALLOWED_ACCOUNT_IDS e o arquivo da conta).")
    return account


def _client(settings: Settings, account: AccountConfig) -> ChatwootClient:
    return ChatwootClient(settings.chatwoot_base_url, account.api_token, account.id, timeout=settings.http_timeout_seconds)


def _check_webhook(client: ChatwootClient, settings: Settings, account: AccountConfig) -> list[tuple[str, str]]:
    expected = f"{settings.public_base_url}/tabule/webhook/{account.id}"
    hooks = [hook for hook in client.webhooks() if hook.get("url") == expected]
    if not hooks:
        return [("ERRO", f"nenhum webhook aponta para {expected}")]
    hook = hooks[0]
    results: list[tuple[str, str]] = []
    if set(hook.get("subscriptions") or []) == {WEBHOOK_EVENT}:
        results.append(("OK", "webhook assina somente conversation_status_changed"))
    else:
        results.append(("ERRO", "o webhook deve assinar somente conversation_status_changed"))
    if hmac.compare_digest(str(hook.get("secret") or "").encode("utf-8"), account.webhook_secret.encode("utf-8")):
        results.append(("OK", "secret do webhook confere com o ambiente"))
    else:
        results.append(("ERRO", "secret do webhook diferente de TABULE_ACCOUNT_<id>_WEBHOOK_SECRET"))
    return results


def _check_account(settings: Settings, account: AccountConfig) -> list[tuple[str, str]]:
    client = _client(settings, account)
    results: list[tuple[str, str]] = []
    try:
        membership = client.account_membership()
        if membership is None:
            return [("ERRO", "o token não tem acesso a esta conta")]
        role = membership.get("role")
        results.append(("OK" if role == "administrator" else "AVISO", f"papel do usuário: {role}"))
        values = attribute_options(client.attribute_definitions(), account.attribute_key)
        results.append(("OK", f"atributo '{account.attribute_key}' com {len(values)} opções"))
        missing = [value for value in values if not account.options.get(value, "").strip()]
        if missing:
            results.append(("ERRO", "opções sem descrição: " + "; ".join(missing)))
        unknown = [value for value in account.options if value not in values]
        if unknown:
            results.append(("AVISO", "descrições de opções que não existem no Chatwoot: " + "; ".join(unknown)))
        titles = {label.get("title") for label in client.account_labels()}
        if account.review_label in titles:
            results.append(("OK", f"label '{account.review_label}' existe"))
        else:
            results.append(("ERRO", f"label '{account.review_label}' não existe nesta conta"))
        if settings.public_base_url is None:
            results.append(("AVISO", "TABULE_PUBLIC_BASE_URL não definido; webhook não verificado"))
        else:
            results.extend(_check_webhook(client, settings, account))
    except RemoteConfigError as exc:
        results.append(("ERRO", str(exc)))
    except (ChatwootError, httpx.HTTPError) as exc:
        results.append(("ERRO", f"falha ao falar com o Chatwoot: {exc}"))
    finally:
        client.close()
    return results


def cmd_check(settings: Settings, accounts: Mapping[int, AccountConfig], errors: Mapping[int, str]) -> int:
    failures = 0
    for account_id in settings.allowed_account_ids:
        if account_id in errors:
            print(f"[ERRO] conta {account_id}: {errors[account_id]}")
            failures += 1
            continue
        account = accounts[account_id]
        print(f"Conta {account_id} ({account.name}):")
        for level, message in _check_account(settings, account):
            print(f"  [{level}] {message}")
            if level == "ERRO":
                failures += 1
    print("Tudo certo." if failures == 0 else f"{failures} problema(s) encontrado(s).")
    return 1 if failures else 0


def cmd_serve(settings: Settings, accounts: Mapping[int, AccountConfig], errors: Mapping[int, str]) -> int:
    app = create_app(settings=settings, accounts=accounts, account_errors=errors)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
    return 0


def cmd_status(settings: Settings) -> int:
    store = Store(settings.db_path)
    try:
        print(f"Fila: {store.queue_summary() or 'vazia'}")
        print("Decisões nas últimas 24 horas:")
        rows = store.decision_summary(since=time.time() - 86400)
        if not rows:
            print("  nenhuma")
        for row in rows:
            print(f"  conta {row['account_id']} | {row['action']}: {row['n']}")
    finally:
        store.close()
    return 0


def cmd_backfill(settings: Settings, accounts: Mapping[int, AccountConfig], args: argparse.Namespace) -> int:
    account = _require_account(accounts, args.account)
    store = Store(settings.db_path)
    pipeline = build_pipeline(settings, accounts, store)
    try:
        since = int(time.time()) - args.days * 86400
        scanned, queued = pipeline.scan_resolved(
            account,
            source="backfill",
            priority=1,
            pages=args.pages,
            min_last_activity=since,
            reset=True,
        )
    finally:
        pipeline.close()
        store.close()
    print(f"Conta {account.id}: {scanned} conversas varridas, {queued} enfileiradas. O serviço processa a fila.")
    return 0


def _sample_human_tabulated(
    client: ChatwootClient,
    account: AccountConfig,
    values: list[str],
    limit: int,
    pages: int,
    exclude: set[int],
) -> list[dict[str, Any]]:
    sample: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        items = client.list_resolved_conversations(page)
        if not items:
            break
        for conversation in items:
            if conversation["id"] in exclude or conversation.get("inbox_id") not in account.inbox_ids:
                continue
            if _human_value(conversation, account.attribute_key) in values:
                sample.append(conversation)
                if len(sample) >= limit:
                    return sample
    return sample


def _print_evaluation(rows: list[EvalRow], values: set[str]) -> None:
    total = len(rows)
    print(f"Conversas avaliadas (só tabulações humanas): {total}")
    if not total:
        return
    print("limiar  cobertura  concordância (nas aceitas)")
    for threshold in THRESHOLDS:
        accepted = [r for r in rows if r.choice in values and r.confidence is not None and r.confidence >= threshold]
        agree = sum(1 for r in accepted if r.choice == r.human)
        accuracy = f"{agree / len(accepted):.1%}" if accepted else "-"
        print(f"{threshold:.2f}    {len(accepted) / total:>8.1%}  {accuracy:>8} ({agree}/{len(accepted)})")
    misses = [r for r in rows if r.choice != r.human]
    if misses:
        print("Divergências (conversa | humano | IA | confiança):")
        for row in misses[:30]:
            confidence = "-" if row.confidence is None else f"{row.confidence:.2f}"
            print(f"  {row.display_id} | {row.human} | {row.choice} | {confidence}")


def cmd_evaluate(settings: Settings, accounts: Mapping[int, AccountConfig], args: argparse.Namespace) -> int:
    account = _require_account(accounts, args.account)
    client = _client(settings, account)
    decisions = DecisionsClient(settings.openai_api_key or "", settings.openai_model, timeout=settings.http_timeout_seconds)
    store = Store(settings.db_path)
    try:
        values = attribute_options(client.attribute_definitions(), account.attribute_key)
        choices = build_choices(values, account.options)
        ai_written = {row["display_id"] for row in store.written_decisions(account.id, since=0)}
        sample = _sample_human_tabulated(client, account, values, args.sample, args.pages, ai_written)
        rows: list[EvalRow] = []
        for conversation in sample:
            transcript = build_transcript(
                client.iter_messages(conversation["id"]),
                max_chars=settings.max_transcript_chars,
                timezone=settings.timezone,
            )
            if not transcript:
                continue
            answer = decisions.choose(
                transcript=transcript,
                instructions=build_instructions(account.context),
                choices=choices,
            )
            rows.append(
                EvalRow(
                    conversation["id"],
                    str(_human_value(conversation, account.attribute_key)),
                    answer.choice,
                    answer.confidence,
                )
            )
    finally:
        client.close()
        decisions.close()
        store.close()
    _print_evaluation(rows, set(values))
    return 0


def cmd_revert(settings: Settings, accounts: Mapping[int, AccountConfig], args: argparse.Namespace) -> int:
    account = _require_account(accounts, args.account)
    since = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
    store = Store(settings.db_path)
    client = _client(settings, account)
    try:
        for decision in store.written_decisions(account.id, since):
            display_id = decision["display_id"]
            current = _human_value(client.get_conversation(display_id), account.attribute_key)
            if current != decision["choice"]:
                print(f"conversa {display_id}: valor atual ({current}) difere da escolha da IA; não será revertida")
                continue
            if not args.execute:
                print(f"conversa {display_id}: seria revertida ({current})")
                continue
            client.destroy_custom_attributes(display_id, [account.attribute_key])
            store.mark_reverted(decision["id"])
            print(f"conversa {display_id}: revertida")
    finally:
        client.close()
        store.close()
    if not args.execute:
        print("Nada foi alterado. Rode de novo com --execute para reverter.")
    return 0


def _parse_inbox_ids(raw: str | None) -> list[int]:
    try:
        ids = [int(part) for part in (raw or "").split(",") if part.strip()]
    except ValueError:
        raise SystemExit(f"--inbox-ids inválido: '{raw}'") from None
    if any(i <= 0 for i in ids):
        raise SystemExit(f"--inbox-ids inválido: '{raw}'")
    return ids


def cmd_describe(settings: Settings, args: argparse.Namespace) -> int:
    """Preenche context e descrições do YAML da conta. Não usa load_accounts: o YAML pode estar incompleto."""
    if args.account not in settings.allowed_account_ids:
        raise SystemExit(f"A conta {args.account} não está em TABULE_ALLOWED_ACCOUNT_IDS.")
    token = os.environ.get(f"TABULE_ACCOUNT_{args.account}_API_TOKEN", "").strip()
    if not token:
        raise SystemExit(f"TABULE_ACCOUNT_{args.account}_API_TOKEN não definido")
    path = settings.accounts_dir / f"{args.account}.yaml"
    try:
        data = accountfile.load(path)
        created = data is None
        if data is None:
            inbox_ids = _parse_inbox_ids(args.inbox_ids)
            if not inbox_ids:
                raise SystemExit(f"{path} não existe: informe --inbox-ids para criá-lo.")
            data = accountfile.skeleton(args.account, name=args.name, inbox_ids=inbox_ids)
        attribute_key = str(data.get("attribute_key") or "tabulacao").strip()
        client = ChatwootClient(settings.chatwoot_base_url, token, args.account, timeout=settings.http_timeout_seconds)
        try:
            values = attribute_options(client.attribute_definitions(), attribute_key)
        finally:
            client.close()
        todo = accountfile.pending(data, values, force=args.force)
        if not todo:
            print("Nada a preencher: context e todas as descrições já estão escritos. Use --force para regenerar.")
            return 0
        writer = DescriptionWriter(
            settings.openai_api_key or "", settings.openai_describe_model, timeout=settings.http_timeout_seconds
        )
        try:
            result = writer.describe(about=args.about, options=values)
        finally:
            writer.close()
        accountfile.apply(data, result.context, result.options, todo)
    except (accountfile.AccountFileError, RemoteConfigError, ChatwootError, DescriberError, httpx.HTTPError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"context: {'gerado' if todo.context else 'mantido'}")
    print(f"descrições geradas: {len(todo.options)} de {len(values)}")
    if args.dry_run:
        print(f"--- {path} (dry-run, nada foi gravado) ---")
        print(accountfile.render(data))
        return 0
    backup = accountfile.save(path, data)
    print(f"{'Criado' if created else 'Atualizado'}: {path}" + (f" (backup em {backup})" if backup else ""))
    print("O texto é um rascunho: revise, rode `tabule check` e mantenha `mode: shadow` até calibrar com `tabule evaluate`.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tabule", description="Tabulação automática de conversas do Chatwoot.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="sobe o serviço: webhook, fila e reconciliação")
    sub.add_parser("check", help="valida a configuração e o acesso de cada conta")
    sub.add_parser("status", help="mostra a fila e as decisões recentes")
    backfill = sub.add_parser("backfill", help="enfileira conversas resolvidas sem tabulação")
    backfill.add_argument("--account", type=int, required=True)
    backfill.add_argument("--days", type=int, default=30)
    backfill.add_argument("--pages", type=int, default=400)
    evaluate = sub.add_parser("evaluate", help="compara a IA com tabulações humanas, sem gravar")
    evaluate.add_argument("--account", type=int, required=True)
    evaluate.add_argument("--sample", type=int, default=40)
    evaluate.add_argument("--pages", type=int, default=20)
    describe = sub.add_parser("describe", help="gera context e descrições das opções no YAML da conta")
    describe.add_argument("--account", type=int, required=True)
    describe.add_argument("--about", required=True, help="texto livre sobre o negócio e o atendimento")
    describe.add_argument("--force", action="store_true", help="regenera também o que já está preenchido")
    describe.add_argument("--dry-run", action="store_true", help="mostra o YAML resultante sem gravar")
    describe.add_argument("--inbox-ids", help="ids das inboxes, separados por vírgula (só se o YAML ainda não existe)")
    describe.add_argument("--name", help="nome da conta (só se o YAML ainda não existe)")
    revert = sub.add_parser("revert", help="desfaz as gravações feitas pela IA")
    revert.add_argument("--account", type=int, required=True)
    revert.add_argument("--since", required=True, help="data inicial, formato AAAA-MM-DD")
    revert.add_argument("--execute", action="store_true", help="sem esta opção, só mostra o que seria desfeito")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(require_openai=args.command in ("serve", "evaluate", "describe"))
    except ConfigError as exc:
        print(f"Configuração inválida: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "describe":
        return cmd_describe(settings, args)
    accounts, errors = load_accounts(settings)
    if args.command == "serve":
        return cmd_serve(settings, accounts, errors)
    if args.command == "check":
        return cmd_check(settings, accounts, errors)
    if args.command == "status":
        return cmd_status(settings)
    if args.command == "backfill":
        return cmd_backfill(settings, accounts, args)
    if args.command == "evaluate":
        return cmd_evaluate(settings, accounts, args)
    return cmd_revert(settings, accounts, args)
