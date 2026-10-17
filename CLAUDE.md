# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projeto

`tabule` tabula automaticamente conversas resolvidas do Chatwoot: lê a transcrição, pede à API Decisions da OpenAI a opção certa do atributo `tabulacao` e grava o resultado (ou marca a conversa com a label `tabulacao-revisar` quando a confiança é baixa). O código, os logs, as mensagens de erro e o README são em **português** — mantenha esse idioma. O README.md tem a referência completa de variáveis de ambiente, comandos da CLI, implantação (systemd + nginx) e rollout.

## Comandos

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup (Python >= 3.10)
.venv/bin/pytest                                              # todos os testes
.venv/bin/pytest tests/test_pipeline.py::test_shadow_mode_never_writes   # um teste
.venv/bin/tabule {check,serve,status,backfill,evaluate,revert}
```

Não há linter configurado. Os testes nunca acessam Chatwoot nem OpenAI: usam `httpx.MockTransport` com `FakeChatwoot`/`FakeDecisions` (`tests/fakes.py`). Comandos manuais da CLI precisam das variáveis `TABULE_*` carregadas (ver `deploy/tabule.env.example`).

## Arquitetura

Um único processo FastAPI (`app.py`, `create_app`) com duas threads em background (`service.Runner`) e uma fila em SQLite (`store.py`).

Fluxo: webhook `conversation_status_changed` → `app.py` valida a assinatura HMAC (`signature.py`, secret por conta), confere conta/evento/status/inbox e enfileira com atraso (`TABULE_RESOLUTION_DELAY_SECONDS`) → thread worker pega o job (`Store.claim_next`) → `Pipeline.process` → grava/registra. Uma segunda thread faz a **reconciliação** periódica (`Pipeline.scan_resolved`), que reenfileira conversas resolvidas das últimas 24 h sem tabulação (o Chatwoot não reenvia webhooks perdidos). `backfill` da CLI usa o mesmo `scan_resolved`.

Peças que exigem ler vários arquivos para entender:

- **`pipeline.py`** é o núcleo. `_process` revalida o estado atual no Chatwoot (resolvida? inbox permitida? já tabulada?), monta a transcrição, chama `DecisionsClient.choose`, decide via `classifier.decide` e aplica em `_apply`. Em `live`, reconsulta a conversa imediatamente antes de gravar (`skipped_changed` se algo mudou). `process` e `scan_resolved` são serializados por um `RLock`, então o pipeline é efetivamente single-threaded. As opções do atributo vêm do Chatwoot (cache de 10 min), não do YAML.
- **Config em duas camadas** (`config.py`): `Settings` vem de variáveis `TABULE_*`; cada conta vem de `/etc/tabule/accounts/<id>.yaml` (modo `shadow`/`live`, `inbox_ids`, limiar, e `options` = descrições por opção) mais segredos `TABULE_ACCOUNT_<id>_API_TOKEN`/`_WEBHOOK_SECRET`. Só contas em `TABULE_ALLOWED_ACCOUNT_IDS` são habilitadas; contas com config inválida vão para `account_errors` (aparecem em `/health` como desabilitadas) em vez de derrubar o serviço.
- **`classifier.py`**: `build_choices` cruza as opções do Chatwoot com as descrições do YAML e **falha** (`OptionsMismatchError`) se alguma opção do Chatwoot não tiver descrição; os nomes no YAML devem bater exatamente com os valores do atributo (aspas no YAML para `SIM`/`NÃO`/`ON`/`OFF`). `decide` devolve `accept` ou `review` (recusa do modelo, opção fora da lista, confiança < limiar).
- **`store.py`**: tabelas `jobs` (chave `(account_id, display_id)`, status `pending/processing/done/failed/not_resolved`, prioridade webhook=0 vs. bulk>0, retry com `retry_later`) e `decisions` (histórico de cada decisão/gravação, base do `revert` e do `status`). `Runner.start` devolve jobs `processing` interrompidos para a fila.
- **Integrações externas** (`chatwoot.py`, `decisions.py`): ambos os clientes aceitam `transport=` para injetar `httpx.MockTransport` nos testes. A Decisions é chamada em `POST /decisions` com uma única pergunta `choice`; resposta `refusal` vira `review`, e qualquer outro tipo inesperado levanta `DecisionsError` (o `Runner` tenta de novo). A assinatura do webhook é `sha256=HMAC(secret, "<timestamp>.<body>")` com tolerância de 300 s (`signature.py`).
- **Erros e retry** (`service.Runner.run_once`): `ChatwootError` 404 marca o job como `failed` sem retry; qualquer outra exceção vai para `Store.retry_later` (backoff, até `max_attempts`=5).
- **`describe`** (`cli.cmd_describe`, `describer.py`, `accountfile.py`): gera `context` e descrições do YAML da conta via Chat Completions (`DescriptionWriter`), sem ler conversas. Propositalmente **não usa `load_accounts`** (um YAML novo/incompleto seria rejeitado por `build_account`); lê o YAML cru com `ruamel.yaml` para preservar comentários e só preenche campos vazios (ou tudo com `--force`).
- **`cli.py`**: `evaluate` compara a IA com tabulações humanas sem gravar nada; `revert` só limpa o atributo se o valor atual ainda for o gravado pela IA e é dry-run sem `--execute`.

Invariantes a preservar: a transcrição nunca é persistida (o banco guarda só fila e decisões); `shadow` não escreve nada no Chatwoot; webhooks de contas fora da allowlist retornam 404 e nunca geram chamadas à API; o payload do webhook precisa ter `account.id` igual ao da URL.
