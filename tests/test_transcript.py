import re

from tabule.transcript import build_transcript

TZ = "America/Sao_Paulo"
STAMP = r"\[\d{2}/\d{2}/\d{4} \d{2}:\d{2}\]"


def test_speakers_attachments_and_activity_messages():
    messages = [
        {"id": 1, "content": "Oi", "message_type": 0, "private": False, "sender_type": "Contact", "created_at": 1760000000},
        {"id": 2, "content": "Olá, tudo bem?", "message_type": 1, "private": False, "sender_type": "User", "created_at": 1760000060},
        {"id": 3, "content": "Interno", "message_type": 1, "private": True, "sender_type": "User", "created_at": 1760000120},
        {"id": 4, "content": "Modelo HSM", "message_type": 3, "private": False, "sender_type": "User", "created_at": 1760000030},
        {"id": 5, "content": "Conversa atribuída", "message_type": 2, "private": False, "sender_type": "User", "created_at": 1760000040},
        {"id": 6, "content": "", "message_type": 0, "private": False, "sender_type": "Contact", "created_at": 1760000050, "attachments": [{"file_type": "image"}]},
        {"id": 7, "content": "Resposta automática", "message_type": 1, "private": False, "sender_type": "AgentBot", "created_at": 1760000070},
    ]
    text = build_transcript(messages, max_chars=10_000, timezone=TZ)
    assert re.search(STAMP + r" Cliente: Oi$", text, re.MULTILINE)
    assert re.search(STAMP + r" Atendente: Olá, tudo bem\?$", text, re.MULTILINE)
    assert re.search(STAMP + r" Nota interna: Interno$", text, re.MULTILINE)
    assert re.search(STAMP + r" Modelo enviado: Modelo HSM$", text, re.MULTILINE)
    assert re.search(STAMP + r" Bot: Resposta automática$", text, re.MULTILINE)
    assert "[anexo: image]" in text
    assert "Conversa atribuída" not in text


def test_multiline_content_is_flattened_to_one_line():
    messages = [{"id": 1, "content": "linha um\nlinha dois", "message_type": 0, "private": False, "sender_type": "Contact", "created_at": 1760000000}]
    text = build_transcript(messages, max_chars=10_000, timezone=TZ)
    assert text.count("\n") == 0
    assert text.endswith("Cliente: linha um / linha dois")


def test_long_conversation_keeps_start_and_end_with_marker():
    messages = [
        {"id": i, "content": f"mensagem {i} " + "x" * 50, "message_type": 0, "private": False, "sender_type": "Contact", "created_at": 1760000000 + i}
        for i in range(1, 201)
    ]
    text = build_transcript(messages, max_chars=2000, timezone=TZ)
    assert "mensagens omitidas" in text
    assert "mensagem 1 " in text
    assert "mensagem 200 " in text
    assert len(text) <= 2100


def test_empty_conversation_produces_empty_transcript():
    assert build_transcript([], max_chars=1000, timezone=TZ) == ""
