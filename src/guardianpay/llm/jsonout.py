"""Extração tolerante do objeto JSON da resposta do modelo (cercas ```json, texto antes/depois, partes em lista),
sem aceitar saída malformada: devolve (obj, None) ou (None, motivo)."""
from __future__ import annotations

import json


def content_text(content) -> str:
    """Normaliza `message.content`: string, lista de partes [{type:text,text}] (alguns gateways) ou dict já decodificado."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return "" if content is None else str(content)


def extract_json(content) -> tuple[dict | None, str | None]:
    if isinstance(content, dict):
        return content, None
    text = content_text(content).strip()
    if not text:
        return None, "resposta vazia"
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
        return (obj, None) if isinstance(obj, dict) else (None, "JSON não é objeto")
    except ValueError:
        pass
    dec = json.JSONDecoder()
    i = text.find("{")
    last_err = "sem objeto JSON"
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                return obj, None
        except ValueError as e:
            last_err = f"JSON inválido ({e.msg})"
        i = text.find("{", i + 1)
    return None, last_err + (" — possivelmente truncado" if text.count("{") > text.count("}") else "")
