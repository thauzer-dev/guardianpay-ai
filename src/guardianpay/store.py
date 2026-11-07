"""Persistência SQLite (stdlib). Três camadas separadas por prefixo:

* raw_*  — cópia fiel (texto) dos CSVs originais. Somente leitura após a ingestão.
* n_*    — dados DERIVADOS/normalizados (centavos, CNPJ só dígitos, boleto decodificado…).
* op_*   — estado operacional MUTÁVEL (cópia de trabalho sobre a qual as ferramentas agem).

Mais as tabelas de controle: events, audit_log, tool_calls (idempotência), cases,
case_transitions, approval_requests, sim_state, ingest_manifest.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_manifest(
  file TEXT PRIMARY KEY, sha256 TEXT NOT NULL, rows INTEGER NOT NULL, raw_table TEXT NOT NULL, ingested_at_sim TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_quality(
  check_id TEXT PRIMARY KEY, severity TEXT NOT NULL, description TEXT NOT NULL, count INTEGER NOT NULL, sample TEXT);
CREATE TABLE IF NOT EXISTS sim_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT UNIQUE NOT NULL,
  sim_ts TEXT NOT NULL,
  type TEXT NOT NULL,
  source TEXT NOT NULL,
  entity_id TEXT,
  payload TEXT NOT NULL,
  trusted INTEGER NOT NULL DEFAULT 0,          -- 0 = conteúdo de terceiros (dado, nunca instrução)
  processed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_events_pending ON events(processed, sim_ts);

CREATE TABLE IF NOT EXISTS audit_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  sim_ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  idempotency_key TEXT,
  decision TEXT NOT NULL,                      -- allowed | denied | requires_approval | executed | failed
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tool_calls(
  idempotency_key TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  actor TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  status TEXT NOT NULL,                        -- started | succeeded | failed | denied | pending_approval
  result TEXT,
  sim_ts TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS cases(
  case_id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  state TEXT NOT NULL,
  titulos TEXT NOT NULL,
  severity TEXT NOT NULL,
  exposure_cents INTEGER NOT NULL,
  certainty TEXT NOT NULL,                     -- indicio | comprovado
  body TEXT NOT NULL,                          -- JSON completo (Finding)
  created_sim_ts TEXT NOT NULL,
  updated_sim_ts TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS case_transitions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL REFERENCES cases(case_id),
  from_state TEXT, to_state TEXT NOT NULL, sim_ts TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approval_requests(
  solicitacao_id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  params TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  required TEXT NOT NULL,                      -- JSON: lista de requisitos (papel/pessoa)
  approvals TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,                        -- pending | approved | rejected | executed | expired
  evidence TEXT NOT NULL,
  created_sim_ts TEXT NOT NULL,
  updated_sim_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS outbox(                -- mensagens a pessoas/fornecedores (efeito SIMULADO; canal externo plugável)
  msg_id TEXT PRIMARY KEY, channel TEXT NOT NULL, to_ref TEXT NOT NULL, kind TEXT NOT NULL,
  body TEXT NOT NULL, titulos TEXT NOT NULL DEFAULT '[]', sim_ts TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS incidents(
  incident_id TEXT PRIMARY KEY, tipo TEXT NOT NULL, causa_provavel TEXT NOT NULL, descricao TEXT NOT NULL,
  titulos TEXT NOT NULL DEFAULT '[]', opened_by TEXT NOT NULL, sim_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS case_decisions(        -- justificativa registrada de cada decisão do agente
  seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, sim_ts TEXT NOT NULL, decision TEXT NOT NULL,
  rationale TEXT NOT NULL, actions TEXT NOT NULL, policy_refs TEXT NOT NULL, llm TEXT NOT NULL DEFAULT 'nao_usado');

CREATE TABLE IF NOT EXISTS lote_closures(          -- efeito SIMULADO do envio do lote ao banco
  lote_remessa TEXT PRIMARY KEY, closed_sim_ts TEXT NOT NULL, signed_by TEXT NOT NULL,
  titulos_enviados INTEGER NOT NULL, valor_enviado_cents INTEGER NOT NULL, titulos_retidos TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sim_payments(
  titulo_id TEXT PRIMARY KEY, lote_remessa TEXT NOT NULL, valor_cents INTEGER NOT NULL, sim_ts TEXT NOT NULL,
  open_cases TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS retained_titles(        -- títulos que deixaram de sair no lote previsto por ação do agente
  titulo_id TEXT PRIMARY KEY, lote_remessa TEXT NOT NULL, valor_cents INTEGER NOT NULL, categoria TEXT NOT NULL,
  case_id TEXT, sim_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS simulated_decisions(     -- SIMULAÇÃO DE TESTE: decisões programadas, nunca manifestação humana real
  seq INTEGER PRIMARY KEY AUTOINCREMENT, solicitacao_id TEXT NOT NULL, actor_id TEXT NOT NULL, decision TEXT NOT NULL,
  scenario TEXT NOT NULL, rule TEXT NOT NULL, sim_ts TEXT NOT NULL, result TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS chat_messages(           -- Agent Workspace: conversa persistida por instância
  seq INTEGER PRIMARY KEY AUTOINCREMENT, sim_ts TEXT NOT NULL, actor_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS chat_pending(             -- pedido de esclarecimento aguardando resposta do colaborador
  actor_id TEXT PRIMARY KEY, intent TEXT NOT NULL, options TEXT NOT NULL, question TEXT NOT NULL, created_sim_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_commands(             -- idempotência de comandos da API (cliques repetidos)
  command_key TEXT PRIMARY KEY, actor_id TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL, created_sim_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_reads(
  msg_id TEXT NOT NULL, actor_id TEXT NOT NULL, PRIMARY KEY(msg_id, actor_id));

CREATE TABLE IF NOT EXISTS sessions(               -- sessões autenticadas: o ator vem da sessão, nunca do cliente
  token_hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL, issued_by TEXT NOT NULL, issued_sim_ts TEXT NOT NULL,
  expires_sim_ts TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
"""

MIGRATIONS = [  # colunas acrescentadas na Entrega 2 (bancos criados na Entrega 1 são migrados sem perda)
    ("cases", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("cases", "last_error", "TEXT"),
    ("cases", "next_check_sim_ts", "TEXT"),
    ("cases", "priority", "REAL NOT NULL DEFAULT 0"),
    ("cases", "pending_request", "TEXT"),
    ("events", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("approval_requests", "routed_to", "TEXT"),
    ("approval_requests", "case_id", "TEXT"),
    ("events", "last_error", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, col, ddl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit; transações explícitas. check_same_thread=False: o acesso é serializado pelo lock da instância (API)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(CONTROL_SCHEMA)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Transação atômica (BEGIN IMMEDIATE). Qualquer exceção faz rollback completo."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM sim_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO sim_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
