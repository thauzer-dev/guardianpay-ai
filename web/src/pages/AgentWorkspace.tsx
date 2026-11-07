import { Bot, Send, User as UserIcon } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Button, ErrorBox, Mono, Spinner } from "../ui";

const SUGGESTIONS = ["Investigue o título TP431754. Cruze os dados de contas a pagar, fornecedor e DDA, identifique inconsistências e explique quais evidências sustentam suas conclusões.",
  "Investigue o título TP431754, cruze os dados e execute as ações autorizadas", "por que o TP431754 foi bloqueado?", "o que está acontecendo com a Embalarte?", "investigue a NF 11094", "esse frete da NF 17319 já não foi pago?",
  "quais ações você executou?", "pendências de aprovação", "reter TP426904", "placar"];

export default function AgentWorkspace() {
  const { iid } = useApp();
  const { data, error, reload } = useLive<any[]>(`/api/instances/${iid}/agent/messages`);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [data?.length]);

  const send = (text: string, e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true); setErr(null);
    api(`/api/instances/${iid}/agent/chat`, { method: "POST", iid, body: { message: text } })
      .then(() => { setMsg(""); reload(); })
      .catch(setErr)
      .finally(() => setBusy(false));
  };

  return (
    <div className="flex h-[calc(100vh-140px)] flex-col gap-3 rise">
      <div>
        <h1 className="text-xl font-semibold text-white">Agente</h1>
        <p className="text-sm text-slate-400">Converse com o orquestrador. Ele só responde com consultas efetivamente executadas (referências <Mono>R#</Mono>) e nunca executa mutação a partir da conversa sem evidência e sem o motor de políticas.</p>
      </div>
      <div className="scroll-thin flex-1 space-y-3 overflow-auto rounded-xl border border-white/5 bg-ink-900/60 p-4" aria-live="polite">
        {!data ? <Spinner /> : data.length === 0 && <p className="text-sm text-slate-400">Comece com uma das sugestões abaixo.</p>}
        {data?.map((m) => (
          <div key={m.seq} className={`flex gap-2.5 ${m.role === "user" ? "flex-row-reverse" : ""}`}>
            <div className={`grid h-7 w-7 shrink-0 place-items-center rounded-full ${m.role === "user" ? "bg-white/10" : "bg-brand-500/20 text-brand-300"}`}>{m.role === "user" ? <UserIcon size={14} aria-hidden /> : <Bot size={14} aria-hidden />}</div>
            <div className={`max-w-[80%] rounded-xl px-3 py-2 text-sm ${m.role === "user" ? "bg-brand-500/15 text-slate-100" : "bg-white/[0.04] text-slate-200 ring-1 ring-inset ring-white/5"}`}>
              <div className="mb-0.5 text-[10px] text-slate-500">{m.actor_id} · {ts(m.sim_ts)}</div>
              {m.role === "agent" && m.payload?.dados?.tarefa_composta ? <Composite d={m.payload.dados} /> : <div className="whitespace-pre-wrap leading-relaxed">{m.content}</div>}
              {m.role === "agent" && m.payload?.intent && (
                <div className="mt-2 flex flex-wrap items-center gap-1 border-t border-white/5 pt-2">
                  <Badge tone="info">intenção: {m.payload.intent}</Badge>
                  <Badge tone={String(m.payload.interpretacao).startsWith("llm") ? "violet" : "neutral"} title={m.payload.interpretacao}>{String(m.payload.interpretacao).startsWith("llm") ? `LLM ${m.payload.interpretacao.slice(4)}` : String(m.payload.interpretacao).split(" · ")[0]}</Badge>
                  {m.payload.referencias?.map((r: any) => <Badge key={r.id} tone={r.status === "ok" ? "ok" : "danger"} title={JSON.stringify(r.params)}>{r.id} {r.tool}{r.status !== "ok" ? ` · ${r.status === "denied" ? "negada" : r.status}` : ""}</Badge>)}
                  {m.payload.dados?.abertos?.length > 0 && <Badge tone="warn">caso aberto: {m.payload.dados.abertos.join(", ")}</Badge>}
                  {String(m.payload.interpretacao).includes("fallback") && <Badge tone="warn" title={m.payload.interpretacao}>LLM indisponível → determinístico</Badge>}
                  {m.payload.dados?.pendente?.opcoes && m.seq === data[data.length - 1].seq && (
                    <div className="mt-1 flex w-full flex-wrap gap-1">{m.payload.dados.pendente.opcoes.map((o: any, i: number) => (
                      <button key={o.id} onClick={() => send(String(i + 1))} className="rounded-md bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200 ring-1 ring-inset ring-amber-500/25 hover:bg-amber-500/20">{i + 1}) {o.label}</button>
                    ))}</div>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <ErrorBox error={error || err} />
      <div className="flex flex-wrap gap-1.5">{SUGGESTIONS.map((s) => <button key={s} onClick={() => send(s)} disabled={busy} className="rounded-full bg-white/5 px-2.5 py-1 text-xs text-slate-300 ring-1 ring-inset ring-white/10 hover:bg-white/10 disabled:opacity-40">{s}</button>)}</div>
      <form onSubmit={(e) => send(msg, e)} className="flex gap-2">
        <label htmlFor="chat" className="sr-only">Mensagem para o agente</label>
        <input id="chat" value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="Ex.: investigar TP432863" className="flex-1 rounded-lg border border-white/10 bg-ink-850 px-3 py-2 text-sm text-slate-100" />
        <Button type="submit" tone="primary" busy={busy}><Send size={14} aria-hidden /> Enviar</Button>
      </form>
    </div>
  );
}


function Sec({ title, children, tone }: { title: string; children: React.ReactNode; tone?: string }) {
  return (
    <div className="mt-2">
      <div className={`mb-1 text-[10px] font-semibold uppercase tracking-wider ${tone || "text-slate-400"}`}>{title}</div>
      {children}
    </div>
  );
}

/** Tarefa composta: separa explicitamente planejado × executado, proposto × realizado, pendente × verificado. */
function Composite({ d }: { d: any }) {
  const rv = d.resultado_verificado || {};
  const evid = Object.entries(d.evidencias || {}) as [string, any][];
  const changed = rv.depois && Object.keys(rv.depois).length > 0;
  return (
    <div className="text-xs leading-relaxed">
      <div className="flex flex-wrap items-center gap-1">
        <Badge tone="info">tarefa composta · {d.titulo}</Badge>
        {(d.objetivos || []).map((o: string) => <Badge key={o} tone="neutral">{o}</Badge>)}
        <Badge tone={String(d.modelo_ia).startsWith("usado") ? "violet" : "warn"} title="Uso real do modelo de IA">IA: {d.modelo_ia}</Badge>
      </div>
      {d.diagnostico_ia && <IaDiag g={d.diagnostico_ia} />}
      <Sec title="Avaliação"><p className="text-sm text-slate-200">{d.avaliacao}</p></Sec>
      {d.inconsistencias?.length > 0 && (
        <Sec title="Inconsistências entre sistemas" tone="text-rose-300">
          <ul className="space-y-0.5">{d.inconsistencias.map((i: any, k: number) => (
            <li key={k}>• {i.sistemas?.length ? <strong>{i.sistemas.join(" × ")}: </strong> : null}{i.descricao} {i.refs?.length ? <Mono>{i.refs.join(", ")}</Mono> : null} <span className="text-slate-500">({i.origem})</span></li>
          ))}</ul>
        </Sec>
      )}
      {d.dados_ausentes?.length > 0 && <Sec title="Dados ausentes" tone="text-amber-300"><div>{d.dados_ausentes.join("; ")}</div></Sec>}
      <div className="grid gap-2 md:grid-cols-2">
        <Sec title="Ferramentas planejadas"><div className="flex flex-wrap gap-1">{(d.ferramentas_planejadas || []).map((t: string, k: number) => <Badge key={k} tone="neutral">{t}</Badge>)}</div></Sec>
        <Sec title="Ferramentas efetivamente executadas"><div className="flex flex-wrap gap-1">{(d.ferramentas_executadas || []).filter((t: any) => t.status === "ok").map((t: any) => <Badge key={t.id} tone="ok">{t.id} {t.tool}</Badge>)}</div>
          {d.ferramentas_negadas?.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{d.ferramentas_negadas.map((t: any, k: number) => <Badge key={k} tone="danger" title={t.motivo}>negada: {t.tool}</Badge>)}</div>}</Sec>
      </div>
      <Sec title={`Evidências coletadas (${evid.length})`}>
        <details><summary className="cursor-pointer text-slate-300">ver evidências do motor de auditoria (ERP, fornecedor, NF, DDA, extrato, e-mail)</summary>
          <ul className="mt-1 space-y-0.5">{evid.map(([k, e]) => <li key={k}><Mono>{k}</Mono> <span className="text-slate-400">{e.fonte}:{e.ref}</span> {e.campo} = <span className="break-all">{String(e.valor)}</span></li>)}</ul>
        </details>
      </Sec>
      <div className="grid gap-2 md:grid-cols-2">
        <Sec title="Ações propostas (IA, validadas)">{d.acoes_propostas?.length ? <ul className="space-y-0.5">{d.acoes_propostas.map((a: any, k: number) => <li key={k}><Mono>{a.tool}</Mono> → {a.resultado}{a.motivo ? <span className="text-slate-400"> ({a.motivo})</span> : null}</li>)}</ul> : <span className="text-slate-500">nenhuma proposta da IA</span>}</Sec>
        <Sec title="Ações realizadas agora (auditoria)" tone="text-emerald-300">{d.acoes_realizadas_agora?.length ? <ul className="space-y-0.5">{d.acoes_realizadas_agora.map((a: any) => <li key={a.seq}>#{a.seq} <Mono>{a.action}</Mono> {a.target} — <Badge tone={a.decision === "executed" ? "ok" : "warn"}>{a.decision === "executed" ? "executada" : "aguarda aprovação"}</Badge> <span className="text-slate-500">{a.actor}</span></li>)}</ul>
          : <span className="text-slate-500">nenhuma ação executada nesta solicitação</span>}</Sec>
      </div>
      {d.acoes_anteriores_do_agente?.length > 0 && (
        <Sec title={`Ações anteriores sobre ${d.titulo} nesta instância (${d.acoes_anteriores_do_agente.length})`}>
          <ul className="space-y-0.5 text-slate-400">{d.acoes_anteriores_do_agente.map((a: any) => <li key={a.seq}>#{a.seq} {ts(a.sim_ts)} <Mono>{a.action}</Mono> — {a.decision === "executed" ? "executada" : "aguarda aprovação"} <span className="text-slate-500">({a.actor}, antes desta solicitação)</span></li>)}</ul>
        </Sec>
      )}
      {d.aprovacoes_pendentes?.length > 0 && (
        <Sec title="Aprovações pendentes" tone="text-amber-300"><ul>{d.aprovacoes_pendentes.map((a: any) => <li key={a.solicitacao_id}><Mono>{a.solicitacao_id}</Mono> {a.tool} → {a.routed_to.map((r: any) => r.candidatos[0]).filter(Boolean).join(", ")}</li>)}</ul></Sec>
      )}
      <Sec title="Resultado verificado (relido do estado operacional)" tone={changed ? "text-emerald-300" : "text-slate-400"}>
        <div>{changed ? <>alterado: <Mono>{JSON.stringify(rv.antes)}</Mono> → <Mono>{JSON.stringify(rv.depois)}</Mono></> : "sem mudança de estado nesta solicitação"} · atual <Mono>{JSON.stringify(rv.estado_atual)}</Mono> <span className="text-slate-500">({rv.lido_por})</span></div>
      </Sec>
      {d.casos?.length > 0 && <div className="mt-1 text-slate-400">Casos: {d.casos.map((c: any) => `${c.case_id} (${c.state})`).join(", ")}</div>}
    </div>
  );
}

/** Diagnóstico da IA nesta tarefa: distingue sucesso, roteador, orçamento, disjuntor, falha de chamada e saída rejeitada. */
function IaDiag({ g }: { g: any }) {
  const tone = g.estado === "usada" ? "text-violet-300" : ["chamada_falhou", "saida_rejeitada"].includes(g.estado) ? "text-rose-300" : "text-amber-300";
  const u = g.uso_nesta_tarefa || {};
  const items: [string, any][] = [
    ["tentou chamar", g.tentou_chamada === undefined ? null : g.tentou_chamada ? "sim" : "não"],
    ["modelo", g.modelo_servido || g.modelo_configurado], ["falha", g.tipo_falha && `${g.tipo_falha}${g.classe_falha ? ` (${g.classe_falha})` : ""}`],
    ["etapa", g.tipo_falha && g.etapa], ["HTTP", g.http_status], ["tentativas", g.tentativas], ["latência", g.latencia_s != null ? `${g.latencia_s}s` : null],
    ["timeout", g.timeout_s ? `${g.timeout_s}s` : null], ["tokens nesta tarefa", u.chamadas ? `${u.tokens_entrada} entrada · ${u.tokens_saida} saída (${u.chamadas} chamada(s))` : null],
    ["orçamento restante", g.orcamento && `${g.orcamento.chamadas_restantes} chamadas · ${g.orcamento.tokens_restantes} tokens`],
    ["disjuntor", g.disjuntor && `${g.disjuntor.estado}${g.disjuntor.causa ? ` — ${g.disjuntor.causa}` : ""}${g.disjuntor.recupera_em_s ? ` · recupera em ${Math.ceil(g.disjuntor.recupera_em_s)}s` : ""}`],
  ];
  return (
    <Sec title={`IA: ${g.descricao}`} tone={tone}>
      {g.motivo_fallback && <div className="text-slate-300">motivo do fallback: {g.motivo_fallback}</div>}
      <div className="flex flex-wrap gap-x-3 text-slate-400">{items.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => <span key={k}>{k}: <span className="text-slate-200">{String(v)}</span></span>)}</div>
    </Sec>
  );
}
