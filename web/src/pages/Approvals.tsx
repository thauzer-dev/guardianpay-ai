import { Check, Lock, Stamp, X } from "lucide-react";
import { useState } from "react";
import { api, brl, newKey, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Button, Card, Empty, ErrorBox, Mono, Ok, Spinner, State } from "../ui";

const TOOL: Record<string, string> = {
  cancelar_titulo: "Cancelar título", corrigir_lancamento: "Corrigir lançamento", agendar_titulo: "Agendar título",
  reverter_alteracao_cadastro: "Reverter alteração bancária", retirar_do_lote: "Retirar do lote",
};

export default function Approvals() {
  const { iid, user, go } = useApp();
  const [tab, setTab] = useState("pending");
  const { data, error, reload } = useLive<any[]>(`/api/instances/${iid}/approvals?status=${tab}`, [tab]);
  const [last, setLast] = useState<any>(null);
  return (
    <div className="space-y-4 rise">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Aprovações</h1>
          <p className="text-sm text-slate-400">Você está decidindo como <strong className="text-slate-200">{user.nome}</strong> ({user.papel}{user.pessoa_id ? ` · ${user.pessoa_id}` : ""}, alçada {user.alcada_cents === null ? "sem limite" : brl(user.alcada_cents)}). A identidade vem da sessão do servidor; segregação e alçada são verificadas no motor de políticas.</p>
        </div>
        <div role="tablist" className="flex gap-1 rounded-lg bg-white/5 p-1 text-xs">
          {[["pending", "Pendentes"], ["executed", "Executadas"], ["approved", "Aprovadas"], ["rejected", "Recusadas"]].map(([k, l]) => (
            <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)} className={`rounded px-2.5 py-1 ${tab === k ? "bg-brand-500/20 text-brand-200" : "text-slate-300"}`}>{l}</button>
          ))}
        </div>
      </div>
      <ErrorBox error={error} />
      {last && (
        <div aria-live="polite">
          <Ok>
            Decisão sobre <Mono>{last.sid}</Mono> registrada: {last.r.resultado.status}{last.r.replayed ? " (clique repetido ignorado)" : ""}.{" "}
            {last.r.agente_retomado ? <>Agente retomado no servidor: {last.r.agente_retomado.acoes} ação(ões) executada(s).</> : null}{" "}
            {last.r.caso && <>Caso <button className="underline" onClick={() => go("cases", last.r.caso.case_id)}>{last.r.caso.case_id}</button>: <State s={last.r.caso.state} /></>}
            {last.r.resultado.reasons?.length > 0 && <div className="text-amber-200">{last.r.resultado.reasons.join("; ")}</div>}
          </Ok>
        </div>
      )}
      {!data ? <Spinner /> : data.length === 0 ? <Empty>Nada nesta caixa.</Empty> : (
        <div className="grid gap-3 xl:grid-cols-2">{data.map((s) => <ApprovalCard key={s.solicitacao_id} s={s} onDone={(r) => { setLast({ sid: s.solicitacao_id, r }); reload(); }} goCase={(c) => go("cases", c)} />)}</div>
      )}
    </div>
  );
}

function ApprovalCard({ s, onDone, goCase }: { s: any; onDone: (r: any) => void; goCase: (c: string) => void }) {
  const { iid } = useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [idem] = useState(newKey);   // mesma chave para cliques repetidos => uma única decisão
  const decide = (kind: "approve" | "reject") => {
    setBusy(true); setErr(null);
    api(`/api/instances/${iid}/approvals/${s.solicitacao_id}/${kind}`, { method: "POST", iid, idem: `${idem}:${kind}`, body: kind === "approve" ? { comentario: text } : { motivo: text || "recusado" } })
      .then((r) => { setRes(r); onDone(r); })
      .catch(setErr)
      .finally(() => setBusy(false));
  };
  return (
    <Card title={<span className="flex items-center gap-2"><Stamp size={14} aria-hidden />{TOOL[s.tool] || s.tool}</span>}
      action={<Badge tone={s.status === "pending" ? "warn" : s.status === "rejected" ? "danger" : "ok"}>{s.status}</Badge>}>
      <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-slate-400">Solicitação</dt><dd><Mono>{s.solicitacao_id}</Mono> {s.case_id && <button className="ml-1 text-brand-300 hover:underline" onClick={() => goCase(s.case_id)}>caso {s.case_id}</button>}</dd>
        <dt className="text-slate-400">Solicitante</dt><dd>{s.requested_by} · {ts(s.created_sim_ts)}</dd>
        <dt className="text-slate-400">Parâmetros</dt><dd className="break-all font-mono text-[11px] text-slate-300">{Object.entries(s.params).filter(([k]) => k !== "evidencias").map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ")}</dd>
        <dt className="text-slate-400">Valor (alçada)</dt><dd className="num">{brl(s.valor_cents)}</dd>
        <dt className="text-slate-400">Exige</dt><dd>{s.required.map((r: any) => <Badge key={JSON.stringify(r)} tone="info">{r.pessoa_id || `papel ${r.role}`}</Badge>)}</dd>
        <dt className="text-slate-400">Roteado para</dt><dd>{s.routed_to.map((r: any, i: number) => <span key={i} className="mr-2">{r.candidatos.join(" / ") || "—"}</span>)}</dd>
      </dl>
      {s.evidence?.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-slate-300">Evidências justificadoras ({s.evidence.length})</summary>
          <ul className="mt-1 space-y-0.5">{s.evidence.map((e: any, i: number) => <li key={i}><Mono>{e.source}:{e.ref}</Mono> <span className="text-slate-400">{e.field}</span> {String(e.value ?? "")}</li>)}</ul>
        </details>
      )}
      {s.approvals.length > 0 && (
        <div className="mt-2 space-y-0.5 text-xs">{s.approvals.map((a: any, i: number) => (
          <div key={i}>✓ {a.actor_id} ({a.role}{a.pessoa_id ? ` · ${a.pessoa_id}` : ""}) {ts(a.sim_ts)} {String(a.comentario || "").startsWith("[SIMULAÇÃO DE TESTE]") && <Badge tone="demo">simulado (teste)</Badge>}</div>
        ))}</div>
      )}
      {s.status === "pending" && (
        <div className="mt-3 border-t border-white/5 pt-3">
          {s.posso_aprovar ? (
            <div className="flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor={`c-${s.solicitacao_id}`}>Comentário ou motivo</label>
              <input id={`c-${s.solicitacao_id}`} value={text} onChange={(e) => setText(e.target.value)} placeholder="Comentário / motivo da recusa"
                className="min-w-0 flex-1 rounded-md border border-white/10 bg-ink-850 px-2 py-1.5 text-xs text-slate-100" />
              <Button tone="primary" busy={busy} onClick={() => decide("approve")}><Check size={14} aria-hidden /> Aprovar</Button>
              <Button tone="danger" busy={busy} onClick={() => decide("reject")} disabled={text.trim().length < 3} title="Informe o motivo (mín. 3 caracteres)"><X size={14} aria-hidden /> Recusar</Button>
            </div>
          ) : (
            <div className="flex items-start gap-2 text-xs text-slate-400"><Lock size={14} className="mt-0.5 shrink-0" aria-hidden />Você não pode decidir esta solicitação: {s.motivo_bloqueio.join("; ")}</div>
          )}
        </div>
      )}
      <div className="mt-2 space-y-2">
        <ErrorBox error={err} />
        {res && (
          <Ok>
            Decisão registrada ({res.resultado.status}{res.replayed ? ", clique repetido ignorado" : ""}).{" "}
            {res.agente_retomado ? <>Agente retomou: {res.agente_retomado.acoes} ação(ões) executada(s).</> : null}{" "}
            {res.caso && <>Caso {res.caso.case_id}: <State s={res.caso.state} /></>}
            {res.resultado.reasons?.length > 0 && <div className="text-amber-200">{res.resultado.reasons.join("; ")}</div>}
          </Ok>
        )}
      </div>
    </Card>
  );
}
