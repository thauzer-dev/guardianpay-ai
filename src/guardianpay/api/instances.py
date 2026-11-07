"""Instâncias isoladas de simulação + execução do agente no servidor.

Cada instância tem seu próprio banco (var/instances/<id>.db), Runtime, lock e thread de execução. O agente roda
no servidor (thread), independentemente de alguma tela estar aberta. Toda operação sobre uma instância é
serializada pelo lock (SQLite + Runtime não são reentrantes); comandos usam chave de idempotência.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, load_settings
from ..log import get_logger, log
from ..runtime import Runtime, bootstrap
from ..store import get_state, set_state, transaction

LOG = get_logger("instances")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class InstanceBusy(RuntimeError):
    pass


@dataclass
class Instance:
    iid: str
    name: str
    rt: Runtime
    lock: threading.RLock = field(default_factory=threading.RLock)
    running: bool = False
    interval_s: float = 1.5
    approval_sim: bool = False
    last_cycle: dict | None = None
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ comandos
    def command(self, key: str | None, actor: str, kind: str, fn):
        """Executa `fn` sob o lock, uma única vez por chave (clique duplicado devolve o mesmo resultado)."""
        if not self.lock.acquire(timeout=10):
            raise InstanceBusy("instância ocupada; tente novamente")
        try:
            if key:
                row = self.rt.conn.execute("SELECT result FROM api_commands WHERE command_key=?", (key,)).fetchone()
                if row:
                    return json.loads(row["result"]) | {"replayed": True}
            result = fn()
            if key:
                with transaction(self.rt.conn):
                    self.rt.conn.execute("INSERT OR IGNORE INTO api_commands VALUES(?,?,?,?,?)",
                                         (key, actor, kind, json.dumps(result, default=str), self.rt.clock.now.isoformat()))
            return result
        finally:
            self.lock.release()

    def step(self) -> dict:
        out = self.rt.step()
        self.last_cycle = out
        return {"executado": out is not None, "sim_now": self.rt.clock.now.isoformat(), "tick": self.rt.clock.tick, "ciclo": out}

    def set_approval_sim(self, enabled: bool) -> None:
        from ..sim.approver import ApprovalSimulator
        self.rt.approval_simulator = ApprovalSimulator(self.rt) if enabled else None
        self.approval_sim = enabled
        with transaction(self.rt.conn):
            set_state(self.rt.conn, "approval_sim", "1" if enabled else "0")


class InstanceManager:
    def __init__(self, base: Settings | None = None, root: Path | None = None, template_root: Path | None = None):
        self.base = base or load_settings()
        self.root = Path(root or self.base.db_path.parent / "instances")
        self.root.mkdir(parents=True, exist_ok=True)
        self.template_root = Path(template_root) if template_root else self.root / ".template"
        self.instances: dict[str, Instance] = {}
        self._mlock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="guardianpay-runner", daemon=True)
        self._thread.start()
        for db in sorted(p for p in self.root.glob("*.db") if not p.name.startswith(".")):     # retoma instâncias existentes (estado persistido)
            try:
                self._open(db.stem, reingest=False)
            except Exception as e:  # noqa: BLE001
                log(LOG, "falha ao reabrir instância", iid=db.stem, error=str(e))

    def _settings(self, iid: str, llm_mode: str | None = None) -> Settings:
        return Settings(**{**self.base.__dict__, "db_path": self.root / f"{iid}.db", "llm_mode": llm_mode or self.base.llm_mode})

    def _template(self) -> Path:
        """Ingestão única (determinística) reutilizada por cópia: criar/reiniciar instância leva ~0,1 s em vez de ~1 s.
        É refeita se qualquer CSV original mudar (hash do manifesto)."""
        import hashlib
        import sqlite3
        from ..ingest import EXPECTED_FILES, sha256
        tdir = self.template_root
        tdir.mkdir(exist_ok=True)
        tpl = tdir / "ingested.db"
        digest = hashlib.sha256("".join(sha256(self.base.raw_dir / f) for f in EXPECTED_FILES).encode()).hexdigest()
        if tpl.exists():
            c = sqlite3.connect(tpl)
            try:
                row = c.execute("SELECT value FROM sim_state WHERE key='raw_digest'").fetchone()
            finally:
                c.close()
            if row and row[0] == digest:
                return tpl
        rt = bootstrap(Settings(**{**self.base.__dict__, "db_path": tpl, "llm_mode": "off"}), reingest=True)
        with transaction(rt.conn):
            set_state(rt.conn, "raw_digest", digest)
        rt.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rt.conn.close()
        return tpl

    def _fresh_copy(self, iid: str) -> None:
        import sqlite3
        dst = self.root / f"{iid}.db"
        for suf in ("", "-wal", "-shm"):
            p = Path(str(dst) + suf)
            if p.exists():
                p.unlink()
        src, out = sqlite3.connect(self._template()), sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            src.close(); out.close()

    def _open(self, iid: str, *, reingest: bool, name: str | None = None, llm_mode: str | None = None) -> Instance:
        if reingest:
            self._fresh_copy(iid)
        rt = bootstrap(self._settings(iid, llm_mode), reingest=False)
        if reingest:
            with transaction(rt.conn):
                set_state(rt.conn, "instance_name", name or iid)
                set_state(rt.conn, "llm_mode", llm_mode or self.base.llm_mode)
        inst = Instance(iid, get_state(rt.conn, "instance_name", iid), rt)
        if get_state(rt.conn, "approval_sim") == "1" and self.base.demo_mode:
            inst.set_approval_sim(True)
        self.instances[iid] = inst
        return inst

    def create(self, name: str, *, llm_mode: str | None = None, approval_sim: bool = False) -> Instance:
        import unicodedata
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        iid = (re.sub(r"[^a-z0-9-]+", "-", ascii_name).strip("-")[:24] or "inst") + "-" + uuid.uuid4().hex[:6]
        if not _ID.match(iid):
            raise ValueError("nome inválido")
        with self._mlock:
            inst = self._open(iid, reingest=True, name=name, llm_mode=llm_mode)
        if approval_sim:
            inst.set_approval_sim(True)
        return inst

    def get(self, iid: str) -> Instance:
        inst = self.instances.get(iid)
        if inst is None:
            raise KeyError(iid)
        return inst

    def reset(self, iid: str) -> Instance:
        old = self.get(iid)
        with old.lock:
            old.running = False
            name, sim, mode = old.name, old.approval_sim, get_state(old.rt.conn, "llm_mode", self.base.llm_mode)
            old.rt.conn.close()
            with self._mlock:
                inst = self._open(iid, reingest=True, name=name, llm_mode=mode)
        if sim:
            inst.set_approval_sim(True)
        return inst

    def delete(self, iid: str) -> None:
        inst = self.get(iid)
        with inst.lock:
            inst.running = False
            inst.rt.conn.close()
            self.instances.pop(iid, None)
            for suf in ("", "-wal", "-shm"):
                p = self.root / f"{iid}.db{suf}"
                if p.exists():
                    p.unlink()

    # ------------------------------------------------------------------ execução no servidor
    def _loop(self) -> None:
        next_due: dict[str, float] = {}
        while not self._stop.is_set():
            now = time.time()
            for iid, inst in list(self.instances.items()):
                if not inst.running or now < next_due.get(iid, 0):
                    continue
                next_due[iid] = now + inst.interval_s
                if not inst.lock.acquire(blocking=False):
                    continue
                try:
                    if inst.rt.clock.finished and get_state(inst.rt.conn, f"cycle_done_{inst.rt.clock.tick}") == "1":
                        inst.running = False
                    else:
                        inst.step()
                        inst.last_error = None
                except Exception as e:  # noqa: BLE001
                    inst.running, inst.last_error = False, f"{type(e).__name__}: {e}"
                    log(LOG, "execução pausada por erro", iid=iid, error=inst.last_error)
                finally:
                    inst.lock.release()
            self._stop.wait(0.1)

    def shutdown(self) -> None:
        self._stop.set()
        for inst in self.instances.values():
            inst.running = False
