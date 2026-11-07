"""Fechamento de lotes no relógio simulado (efeito SIMULADO do envio ao banco).

No horário de corte (10:00 Meridiano / 14:00 Litoral, só dia útil): o que está no lote e não está bloqueado
"sai" (status pago); título bloqueado é retido. Se nenhum aprovador assinou, o tesoureiro assina sem conferir
(situação atual descrita na política). Depois do fechamento, nenhuma retirada é aceita (PolicyEngine).
Lotes que fecham depois do fim da simulação não são enviados: são avaliados no estado final.
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import audit, events
from ..log import get_logger, log
from ..store import transaction

LOG = get_logger("scheduler")


def close_due_lots(rt) -> list[str]:
    now = rt.clock.now
    closed = []
    lots = rt.conn.execute("SELECT * FROM op_lotes WHERE status='aberto' ORDER BY data_prevista, horario_fechamento").fetchall()
    for lote in lots:
        close = datetime.fromisoformat(f"{lote['data_prevista']}T{lote['horario_fechamento']}:00")
        if close > now or close > rt.clock.end:
            continue
        if not rt.calendar.is_business_day(close.date()):
            continue  # sem expediente bancário: não há envio
        lid, ts = lote["lote_remessa"], close.isoformat()
        with transaction(rt.conn):
            n = rt.conn.execute("UPDATE op_lotes SET status='enviado', liberado_por=coalesce(liberado_por,'usuario_14'), "
                                "version=version+1 WHERE lote_remessa=? AND status='aberto'", (lid,)).rowcount
            if n != 1:
                continue  # já fechado por outro ciclo (idempotente)
            sent = rt.conn.execute("SELECT o.*, n.categoria FROM op_titulos o JOIN n_titulos n USING(titulo_id) "
                                   "WHERE o.lote_remessa=? AND o.status='agendado' AND o.bloqueado=0", (lid,)).fetchall()
            held = rt.conn.execute("SELECT titulo_id FROM op_titulos WHERE lote_remessa=? AND status='agendado' AND bloqueado=1", (lid,)).fetchall()
            pagos_com_caso = []
            for t in sent:
                rt.conn.execute("UPDATE op_titulos SET status='pago', data_pagamento=?, version=version+1 WHERE titulo_id=?",
                                (lote["data_prevista"], t["titulo_id"]))
                cases = [r["case_id"] for r in rt.conn.execute(
                    "SELECT case_id FROM cases WHERE titulos LIKE ? AND state<>'resolved'", (f'%"{t["titulo_id"]}"%',))]
                if cases:
                    pagos_com_caso.append(t["titulo_id"])
                rt.conn.execute("INSERT OR IGNORE INTO sim_payments(titulo_id,lote_remessa,valor_cents,sim_ts,open_cases) VALUES(?,?,?,?,?)",
                                (t["titulo_id"], lid, t["valor_cents"], ts, json.dumps(cases)))
            for h in held:   # bloqueado: não sai; volta para aberto mantendo o bloqueio
                rt.conn.execute("UPDATE op_titulos SET status='aberto', lote_remessa='', version=version+1 WHERE titulo_id=?", (h["titulo_id"],))
            # títulos que estavam na agenda original deste lote e não saíram (retidos pelo agente)
            for r in rt.conn.execute("SELECT a.titulo_id, a.valor_cents, n.categoria FROM n_agenda a JOIN n_titulos n USING(titulo_id) "
                                     "JOIN op_titulos o USING(titulo_id) WHERE a.lote_remessa=? AND o.status<>'pago'", (lid,)).fetchall():
                case = rt.conn.execute("SELECT case_id FROM cases WHERE titulos LIKE ? ORDER BY created_sim_ts LIMIT 1",
                                       (f'%"{r["titulo_id"]}"%',)).fetchone()
                rt.conn.execute("INSERT OR IGNORE INTO retained_titles(titulo_id,lote_remessa,valor_cents,categoria,case_id,sim_ts) VALUES(?,?,?,?,?,?)",
                                (r["titulo_id"], lid, r["valor_cents"], r["categoria"], case["case_id"] if case else None, ts))
            total = sum(t["valor_cents"] for t in sent)
            rt.conn.execute("INSERT OR IGNORE INTO lote_closures VALUES(?,?,?,?,?,?)",
                            (lid, ts, "usuario_14", len(sent), total, json.dumps([h["titulo_id"] for h in held])))
            audit.append(rt.conn, sim_ts=ts, actor="sistema:banco", actor_role="simulacao", action="fechamento_lote", target=lid,
                         decision="executed", payload={"enviados": len(sent), "valor_cents": total, "retidos": len(held),
                                                       "provenance": "simulado"})
            events.emit(rt.conn, type_="lote.fechado", source="banco", sim_ts=ts, entity_id=lid, trusted=True,
                        payload={"lote": lid, "enviados": len(sent), "pagos_com_caso": pagos_com_caso})
        log(LOG, "lote fechado", lote=lid, enviados=len(sent), valor=total / 100, retidos=len(held))
        closed.append(lid)
    return closed
