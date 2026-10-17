from pathlib import Path

from tabule import accountfile
from tabule.config import Settings, load_accounts

EXAMPLE = Path(__file__).resolve().parent.parent / "deploy" / "accounts" / "example.yaml"
VALUES = ["IMPRODUTIVO JA E ALUNO", "RECUSA FINANCEIRO", "SIM"]
DESCRIPTIONS = {value: f"descrição de {value}" for value in VALUES}


def example(tmp_path):
    path = tmp_path / "9.yaml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_pending_fills_placeholder_context_and_empty_options():
    data = accountfile.load(EXAMPLE)
    todo = accountfile.pending(data, VALUES, force=False)
    assert todo.context
    assert todo.options == VALUES


def test_does_not_overwrite_existing_text(tmp_path):
    path = example(tmp_path)
    data = accountfile.load(path)
    data["context"] = "Meu contexto."
    data["options"]["RECUSA FINANCEIRO"] = "Minha descrição."
    todo = accountfile.pending(data, VALUES, force=False)
    assert not todo.context
    assert todo.options == ["IMPRODUTIVO JA E ALUNO", "SIM"]
    accountfile.apply(data, "Contexto da IA.", DESCRIPTIONS, todo)
    assert data["context"] == "Meu contexto."
    assert data["options"]["RECUSA FINANCEIRO"] == "Minha descrição."
    assert data["options"]["IMPRODUTIVO JA E ALUNO"] == "descrição de IMPRODUTIVO JA E ALUNO"


def test_force_regenerates_everything(tmp_path):
    data = accountfile.load(example(tmp_path))
    data["context"] = "Meu contexto."
    data["options"]["RECUSA FINANCEIRO"] = "Minha descrição."
    todo = accountfile.pending(data, VALUES, force=True)
    accountfile.apply(data, "Contexto da IA.", DESCRIPTIONS, todo)
    assert data["context"].strip() == "Contexto da IA."
    assert data["options"]["RECUSA FINANCEIRO"] == "descrição de RECUSA FINANCEIRO"


def test_nothing_pending_when_everything_is_written(tmp_path):
    data = accountfile.load(example(tmp_path))
    data["context"] = "Contexto."
    data["options"] = {value: "texto" for value in VALUES}
    assert not accountfile.pending(data, VALUES, force=False)


def test_new_remote_option_is_added_with_quoted_boolean_like_key(tmp_path):
    path = example(tmp_path)
    data = accountfile.load(path)
    todo = accountfile.pending(data, [*VALUES, "NÃO"], force=False)
    accountfile.apply(data, "Contexto.", {**DESCRIPTIONS, "NÃO": "nao"}, todo)
    accountfile.save(path, data)
    text = path.read_text(encoding="utf-8")
    assert '"NÃO": nao' in text
    assert '"SIM": ' in text
    assert accountfile.load(path)["options"]["SIM"] == "descrição de SIM"


def test_save_keeps_comments_order_and_creates_backup(tmp_path):
    path = example(tmp_path)
    original = path.read_text(encoding="utf-8")
    data = accountfile.load(path)
    accountfile.apply(data, "Contexto.", DESCRIPTIONS, accountfile.pending(data, VALUES, force=False))
    backup = accountfile.save(path, data)
    text = path.read_text(encoding="utf-8")
    assert backup.read_text(encoding="utf-8") == original
    assert "# shadow: só registra | live: grava no Chatwoot" in text
    assert text.index("name:") < text.index("mode:") < text.index("context:") < text.index("options:")
    assert not (tmp_path / "9.yaml.tmp").exists()


def test_skeleton_is_shadow_and_result_loads_as_account(tmp_path):
    data = accountfile.skeleton(9, name="FDC", inbox_ids=[16, 27])
    accountfile.apply(data, "Contexto.", DESCRIPTIONS, accountfile.pending(data, VALUES, force=False))
    path = tmp_path / "9.yaml"
    assert accountfile.save(path, data) is None
    env = {
        "TABULE_ALLOWED_ACCOUNT_IDS": "9",
        "TABULE_CHATWOOT_BASE_URL": "https://omni.example",
        "TABULE_ACCOUNTS_DIR": str(tmp_path),
    }
    accounts, errors = load_accounts(
        Settings.from_env(env, require_openai=False),
        environ={**env, "TABULE_ACCOUNT_9_API_TOKEN": "t", "TABULE_ACCOUNT_9_WEBHOOK_SECRET": "s"},
    )
    assert errors == {}
    assert accounts[9].mode == "shadow"
    assert accounts[9].name == "FDC"
    assert accounts[9].inbox_ids == frozenset({16, 27})
    assert accounts[9].options == {value: DESCRIPTIONS[value] for value in VALUES}
