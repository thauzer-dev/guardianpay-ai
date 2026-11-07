"""Configuração por ambiente. Nenhum segredo é embutido no código."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Carrega .env simples (KEY=VALUE) sem sobrescrever variáveis já definidas."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():   # utf-8-sig: tolera BOM de editores no Windows
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    raw_dir: Path
    db_path: Path
    sim_start: datetime
    sim_ticks: int
    ai_proxy_base_url: str
    ai_proxy_model: str
    ai_proxy_model_preference: str = ""
    llm_mode: str = "auto"  # auto | off
    demo_mode: bool = False  # habilita login simulado (CLI/simulador). NUNCA em produção
    ai_proxy_api_key: str | None = field(repr=False, default=None)  # nunca logado

    @property
    def llm_enabled(self) -> bool:
        return bool(self.ai_proxy_api_key) and self.llm_mode != "off"


def load_settings(**overrides) -> Settings:
    _load_dotenv(ROOT / ".env")
    s = dict(
        raw_dir=Path(os.environ.get("GP_RAW_DIR", ROOT / "data" / "raw")),
        db_path=Path(os.environ.get("GP_DB_PATH", ROOT / "var" / "guardianpay.db")),
        sim_start=datetime.fromisoformat(os.environ.get("GP_SIM_START", "2026-11-02T08:00:00")),
        sim_ticks=int(os.environ.get("GP_SIM_TICKS", "168")),
        ai_proxy_base_url=os.environ.get("AI_PROXY_BASE_URL", "https://ai-proxy.gogroupbr.com/v1"),
        ai_proxy_model=os.environ.get("AI_PROXY_MODEL", ""),  # vazio = escolher entre /v1/models
        ai_proxy_model_preference=os.environ.get("AI_PROXY_MODEL_PREFERENCE", ""),
        llm_mode=os.environ.get("GP_LLM_MODE", "auto"),
        demo_mode=os.environ.get("GP_DEMO_MODE", "0") == "1",
        ai_proxy_api_key=os.environ.get("AI_PROXY_API_KEY") or None,
    )
    s.update(overrides)
    return Settings(**s)
