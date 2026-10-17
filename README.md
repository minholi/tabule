# tabule

Tabulação automática de conversas do Chatwoot.

Quando uma conversa é resolvida, o `tabule` lê a transcrição, pede à API Decisions da OpenAI a opção certa do atributo `tabulacao` (com as opções lidas do próprio Chatwoot) e grava o resultado. Se a confiança for baixa, a conversa recebe a label `tabulacao-revisar` e fica para revisão humana.

## Como funciona

1. O Chatwoot envia `conversation_status_changed` para `/tabule/webhook/<account_id>`.
2. O serviço valida a assinatura HMAC com o secret da conta, responde `200` na hora e enfileira a conversa.
3. Depois de `TABULE_RESOLUTION_DELAY_SECONDS` (padrão 60 s), o worker:
   - confirma que a conversa continua resolvida e sem tabulação;
   - monta a transcrição com todas as mensagens (paginando a API), incluindo notas internas e templates enviados;
   - classifica com a Decisions, usando as opções do Chatwoot e as descrições do arquivo da conta;
   - no modo `shadow`, só registra a decisão; no modo `live`, grava `tabulacao` (com `merge: true`) ou adiciona a label de revisão.
4. A cada 15 min, a reconciliação procura conversas resolvidas nas últimas 24 h sem tabulação que ainda não entraram na fila (cobre webhooks perdidos, porque o Chatwoot não reenvia).

A transcrição não é guardada. O banco SQLite registra só a fila, o resultado de cada decisão e o que foi gravado (para reversão).

## Requisitos

- Python 3.10 ou superior
- Chatwoot com API e webhooks, e um usuário técnico com acesso a cada conta
- Chave da OpenAI com acesso à API Decisions (beta, modelo `gpt-6-luna`)

## Desenvolvimento

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Os testes não acessam Chatwoot nem OpenAI: usam transportes simulados.

## Configuração

### Variáveis de ambiente (`/etc/tabule/tabule.env`)

Modelo em `deploy/tabule.env.example`.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TABULE_ALLOWED_ACCOUNT_IDS` | sim | Lista de contas permitidas, ex.: `9,12`. Conta fora da lista é recusada (404) e nunca chamada na API. |
| `TABULE_CHATWOOT_BASE_URL` | sim | URL base do Chatwoot, ex.: `https://omni.maisa.educa.dev.br` |
| `TABULE_OPENAI_API_KEY` | sim (serve e evaluate) | Chave da OpenAI |
| `TABULE_PUBLIC_BASE_URL` | recomendada | URL pública do serviço; usada pelo `check` para verificar o webhook |
| `TABULE_ACCOUNT_<id>_API_TOKEN` | sim, por conta | Token do usuário técnico na conta |
| `TABULE_ACCOUNT_<id>_WEBHOOK_SECRET` | sim, por conta | Campo `secret` do webhook da conta |
| `TABULE_OPENAI_MODEL` | não | Padrão `gpt-6-luna` |
| `TABULE_OPENAI_DESCRIBE_MODEL` | não | Modelo de texto (Chat Completions) usado por `tabule describe`. Padrão: o mesmo de `TABULE_OPENAI_MODEL` |
| `TABULE_ACCOUNTS_DIR` | não | Padrão `/etc/tabule/accounts` |
| `TABULE_DB_PATH` | não | Padrão `/var/lib/tabule/tabule.db` |
| `TABULE_TIMEZONE` | não | Fuso usado nas datas da transcrição. Padrão `America/Sao_Paulo` |
| `TABULE_HOST` / `TABULE_PORT` | não | Padrão `127.0.0.1:8088` |
| `TABULE_RESOLUTION_DELAY_SECONDS` | não | Espera após a resolução. Padrão `60` |
| `TABULE_RECONCILE_INTERVAL_SECONDS` / `TABULE_RECONCILE_PAGES` | não | Padrões `900` e `4` |
| `TABULE_MAX_TRANSCRIPT_CHARS` | não | Teto da transcrição. Padrão `40000` |
| `TABULE_LOG_LEVEL` | não | Padrão `INFO` |

### Arquivo por conta (`/etc/tabule/accounts/<id>.yaml`)

Modelo em `deploy/accounts/example.yaml`.

```yaml
name: "FDC"
mode: shadow                  # shadow: só registra | live: grava
inbox_ids: [16, 27, 37]       # inboxes que podem ser tabuladas
attribute_key: tabulacao
review_label: tabulacao-revisar
confidence_threshold: 0.8
context: >
  Pós-graduação e bootcamps, atendimento por WhatsApp.
options:
  "RECUSA FINANCEIRO": "Cliente recusou por questão de dinheiro ou parcelamento."
  "PAROU DE RESPONDER PRODUTO": "..."
```

- Os nomes das opções devem ser **exatamente** os valores do atributo no Chatwoot. Ponha entre aspas (no YAML, `SIM`, `NÃO`, `ON` e `OFF` sem aspas viram booleanos).
- Toda opção do Chatwoot precisa de descrição (o `tabule describe` gera um rascunho). Descrição vazia impede a classificação daquela conta até ser preenchida. O `tabule check` aponta quais faltam.
- Opções com descrição mas inexistentes no Chatwoot geram aviso no `check`.

## Comandos

| Comando | O que faz |
|---|---|
| `tabule check` | Valida token, papel do usuário, atributo e opções, label de revisão e webhook (URL, assinatura e secret). Sai com código 1 se houver erro. |
| `tabule serve` | Sobe o serviço (webhook, fila e reconciliação). É o que o systemd executa. |
| `tabule status` | Mostra a fila e as decisões das últimas 24 h. |
| `tabule describe --account 9 --about "..."` | Gera, com a OpenAI, o `context` e as descrições das opções do YAML da conta, a partir dos nomes das opções (lidos do Chatwoot) e do texto livre de `--about`. **Não sobrescreve** o que já está preenchido (`--force` regenera tudo; `--dry-run` só mostra). Se o YAML não existe, cria um em `shadow` (exige `--inbox-ids`, aceita `--name`). Faz backup em `<id>.yaml.bak`. O texto é um rascunho: revise. |
| `tabule backfill --account 9 --days 30` | Enfileira conversas resolvidas sem tabulação. Reprocessa as que já estavam na fila como concluídas, então pode ser rodado de novo ao passar de `shadow` para `live`. |
| `tabule evaluate --account 9 --sample 40` | Compara a IA com tabulações **feitas por pessoas** (ignora o que a IA gravou), sem gravar nada. Mostra cobertura e concordância por limiar, para escolher `confidence_threshold`. |
| `tabule revert --account 9 --since 2026-10-17` | Mostra o que seria desfeito. Com `--execute`, limpa `tabulacao` nas conversas que a IA gravou, só se o valor atual ainda for o da IA. |

A API HTTP do serviço tem duas rotas:

- `POST /tabule/webhook/<account_id>`: a única rota publicada pelo nginx.
- `GET /health`: status, contas e fila. Use só localmente (`curl http://127.0.0.1:8088/health`).

## Implantação no servidor

Assumindo Ubuntu, Chatwoot instalado a partir do script oficial (nginx na frente, serviços `chatwoot-web.1` e `chatwoot-worker.1`).

1. **Usuário e pastas**

   ```bash
   sudo useradd --system --home /var/lib/tabule --shell /usr/sbin/nologin tabule
   sudo mkdir -p /opt/tabule /etc/tabule/accounts /var/lib/tabule
   sudo chown tabule:tabule /var/lib/tabule
   ```

2. **Código e dependências** (copie o projeto para `/opt/tabule`)

   ```bash
   cd /opt/tabule
   python3 -m venv .venv
   .venv/bin/pip install .
   ```

3. **Configuração**

   ```bash
   sudo cp deploy/tabule.env.example /etc/tabule/tabule.env
   sudo cp deploy/accounts/example.yaml /etc/tabule/accounts/9.yaml
   sudo chown root:tabule /etc/tabule/tabule.env && sudo chmod 640 /etc/tabule/tabule.env
   ```

   Preencha o `.env` (tokens e chave da OpenAI) e o YAML da conta (`tabule describe --account <id> --about "..."` gera um rascunho do `context` e das descrições; revise o texto) e deixe `mode: shadow`. O campo `TABULE_ACCOUNT_<id>_WEBHOOK_SECRET` só existe depois de criar o webhook (passo 7). Até lá, use um valor provisório, como `temporario`. Sem um valor, a conta fica desabilitada.

   Os comandos manuais precisam do mesmo ambiente do serviço:

   ```bash
   sudo -u tabule bash -c 'set -a; . /etc/tabule/tabule.env; set +a; /opt/tabule/.venv/bin/tabule <comando>'
   ```

4. **Validação antes do webhook**

   ```bash
   sudo -u tabule bash -c 'set -a; . /etc/tabule/tabule.env; set +a; /opt/tabule/.venv/bin/tabule check'
   ```

   Nesse ponto, o `check` deve mostrar apenas o erro de webhook não encontrado (e o aviso do secret provisório). Token, atributo, opções e label devem estar OK.

5. **Nginx**: descubra o arquivo com `sudo nginx -T | grep -n server_name`, faça backup e insira `deploy/nginx-tabule.conf` dentro do bloco `server` de `omni.maisa.educa.dev.br` (porta 443), antes do `location /`.

   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

   Testes de rota:

   - `curl -i https://omni.maisa.educa.dev.br/tabule/webhook/9` → 403 (nginx: só POST)
   - `curl -i -X POST https://omni.maisa.educa.dev.br/tabule/webhook/9 -d '{}'` → 401 (aplicação: sem assinatura)
   - `curl -i https://omni.maisa.educa.dev.br/tabule/health` → 404 (não publicado)

6. **Serviço**

   ```bash
   sudo cp deploy/tabule.service /etc/systemd/system/tabule.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now tabule
   journalctl -u tabule -f
   ```

7. **Webhook por conta**: em Configurações → Integrações → Webhooks, para cada conta permitida, use a URL `https://omni.maisa.educa.dev.br/tabule/webhook/<id>` e assine **somente** `conversation_status_changed`. Copie o campo `secret` para `TABULE_ACCOUNT_<id>_WEBHOOK_SECRET` e reinicie o serviço.

8. **Verificação final**: `sudo -u tabule /opt/tabule/.venv/bin/tabule check` deve terminar em "Tudo certo.".

9. **Teste ponta a ponta**: resolva uma conversa de teste em uma inbox da lista. Em `journalctl -u tabule` deve aparecer o enfileiramento e, em seguida, a decisão (`would_write`, `would_review` etc.). Se a entrega falhar, procure `Invalid webhook URL` em `journalctl -u chatwoot-worker.1.service`. Isso indica que o DNS do domínio resolve para IP privado.

### Rollout

1. **Shadow**: rode `tabule backfill --account 9 --days 30` e acompanhe com `tabule status`.
2. **Calibração**: rode `tabule evaluate --account 9 --sample 40`, escolha o `confidence_threshold` com base na concordância e ajuste o YAML.
3. **Descrições**: revise as descrições das opções com base nas divergências do `evaluate`.
4. **Live**: troque `mode: shadow` por `mode: live`, reinicie o serviço e rode `tabule backfill --account 9 --days 30` de novo para aplicar o histórico.

Para adicionar uma conta depois: adicione o id em `TABULE_ALLOWED_ACCOUNT_IDS`, crie o YAML, preencha os segredos, crie a label `tabulacao-revisar` e o webhook na conta, rode `tabule check` e reinicie o serviço. A conta começa em `shadow`.

## Rollback

- Parar tudo: `sudo systemctl stop tabule` (e desativar os webhooks no Chatwoot).
- Desfazer gravações da IA: `tabule revert --account 9 --since AAAA-MM-DD` (ver a lista) e depois com `--execute`.
- Remover uma conta: tire o id de `TABULE_ALLOWED_ACCOUNT_IDS` e reinicie.

## Operação

- **Jobs com falha**: `tabule status` mostra jobs `failed`. Corrija a causa (token, descrição, opção) e rode `tabule backfill` no período.
- **Webhook perdido** (serviço parado, por exemplo): a reconciliação cobre as últimas 24 h. Para períodos maiores, use `backfill`.
- **Upgrade do Chatwoot**: confira depois se o bloco do nginx continua no lugar.
- **Dados pessoais**: a transcrição vai para a OpenAI. Confirme DPA/ZDR da conta antes do modo `live`.

## Limitações conhecidas

- Um único processo com fila em SQLite. É suficiente para o volume esperado, mas não escala horizontalmente.
- As opções do atributo são lidas do Chatwoot e ficam em cache por 10 min.
- Os testes automatizados usam Chatwoot e OpenAI simulados. A primeira validação com as APIs reais é o `check` e o modo `shadow` no servidor.
