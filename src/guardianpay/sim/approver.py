"""Simulador de respostas de aprovação — EXCLUSIVO PARA TESTE/DEMONSTRAÇÃO.

* Decisões vêm de um cenário explícito (JSON versionado), nunca de inferência.
* Usa identidades de teste do cadastro (usuarios_sistema) com sessões de demonstração ('demo:simulador-teste'),
  passando pelo mesmo caminho de um humano: sessão -> aprovar_acao/recusar_acao -> PolicyEngine.
* Só responde em horário de expediente (dia útil, 08h–18h) e após o atraso configurado.
* Cada decisão fica registrada em `simulated_decisions` e o comentário leva o prefixo [SIMULAÇÃO DE TESTE].
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..auth import execute_with_session
from ..tools.contract import ToolStatus

DEFAULT_SCENARIO = Path(__file__).with_name("scenarios") / "demo_aprovacoes.json"
TAG = "[SIMULAÇÃO DE TESTE]"


class ApprovalSimulator:
    def __init__(self, rt, scenario_path: Path | None = None):
        if not rt.settings.demo_mode:
            raise PermissionError("simulador de aprovação só roda em modo demonstração (GP_DEMO_MODE=1)")
        self.rt = rt
        self.scenario = json.loads(Path(scenario_path or DEFAULT_SCENARIO).read_text(encoding="utf-8"))

    def _match(self, sol) -> dict | None:
        params = json.loads(sol["params"])
        oc_status = None
        if params.get("titulo_id"):
            r = self.rt.conn.execute("SELECT c.status FROM op_titulos o LEFT JOIN n_oc c ON c.oc_id=o.oc_id WHERE o.titulo_id=?",
                                     (params["titulo_id"],)).fetchone()
            oc_status = r["status"] if r else None
        for rule in self.scenario["regras"]:
            m = rule["match"]
            if m.get("tool") != sol["tool"]:
                continue
            if "campo" in m and params.get("campo") != m["campo"]:
                continue
            if "oc_status" in m and oc_status != m["oc_status"]:
                continue
            return rule
        return None

    def tick(self) -> list[dict]:
        now = self.rt.clock.now
        if not self.rt.calendar.is_team_working(now):
            return []
        done = []
        for sol in self.rt.conn.execute("SELECT * FROM approval_requests WHERE status='pending' ORDER BY created_sim_ts, solicitacao_id").fetchall():
            rule = self._match(sol)
            if rule is None or datetime.fromisoformat(sol["created_sim_ts"]) + timedelta(hours=rule["atraso_horas"]) > now:
                continue
            routing = json.loads(sol["routed_to"] or "[]")
            approvals = json.loads(sol["approvals"])
            used = {a["actor_id"] for a in approvals}
            for item in routing:
                if self.rt.conn.execute("SELECT status FROM approval_requests WHERE solicitacao_id=?", (sol["solicitacao_id"],)).fetchone()["status"] != "pending":
                    break
                who = next((c for c in item["candidatos"] if c not in used), None)
                if who is None:
                    continue
                token = self.rt.sessions.issue_demo(who, purpose="simulador-teste")
                try:
                    if rule["decisao"] == "aprovar":
                        res = execute_with_session(self.rt, token, "aprovar_acao", {"solicitacao_id": sol["solicitacao_id"],
                                                   "comentario": f"{TAG} cenário {self.scenario['nome']}/{rule['id']}"})
                    else:
                        res = execute_with_session(self.rt, token, "recusar_acao", {"solicitacao_id": sol["solicitacao_id"],
                                                   "motivo": f"{TAG} {rule.get('motivo', 'recusa programada')}"})
                finally:
                    self.rt.sessions.revoke(token)
                used.add(who)
                self.rt.conn.execute("INSERT INTO simulated_decisions(solicitacao_id,actor_id,decision,scenario,rule,sim_ts,result) VALUES(?,?,?,?,?,?,?)",
                                     (sol["solicitacao_id"], who, rule["decisao"], self.scenario["nome"], rule["id"], now.isoformat(),
                                      f"{res.status.value} {res.reasons}"[:300]))
                done.append({"solicitacao_id": sol["solicitacao_id"], "ator_teste": who, "decisao": rule["decisao"],
                             "resultado": res.status.value})
                if rule["decisao"] != "aprovar" or res.status is not ToolStatus.ok:
                    break
        return done
