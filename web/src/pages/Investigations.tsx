import { ArrowLeft, Bot, FileSearch, GitBranch, ListChecks, Scale, Stamp, Users } from "lucide-react";
import { useState } from "react";
import { brl, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Card, Certainty, Empty, ErrorBox, Mono, Spinner, State, sevTone } from "../ui";

const RULES: Record<string, string> = {
  R1_DUPLICIDADE: "Duplicidade", R1_DUPLICIDADE_HISTORICA: "Duplicidade histórica", R2_VALOR_ACIMA_DUPLICATA: "Valor acima da duplicata",
  R2_VALOR_ABAIXO_DUPLICATA: "Valor abaixo da duplicata", R2_NF_ACIMA_OC: "NF acima da OC", R3_PAGO_ANTERIORMENTE: "Já pago fora do lote",
  R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA: "Troca bancária sem confirmação", R5_BENEFICIARIO_INCOMPATIVEL: "Beneficiário incompatível",
  R5_FORNECEDOR_DIVERGENTE: "Fornecedor errado", R6_APROVACAO_DUPLA_AUSENTE: "Aprovação dupla ausente", R6_FRACIONAMENTO: "Fracionamento",
  R7_COMUNICACAO_SUSPEITA: "Comunicação suspeita",
};
export const ruleLabel = (r: string) => RULES[r] || r;
const SOURCES: Record<string, string> = {
  op_titulos: "ERP · títulos (estado atual)", n_titulos: "ERP · títulos", n_duplicatas: "Portal NF-e · duplicatas", n_nf: "Portal NF-e · notas",
  n_oc: "Compras · pedidos", n_dda: "DDA · boletos registrados", n_extrato: "Extratos bancários", raw_alteracoes_cadastro: "ERP · histórico de cadastro",
  raw_comunicacoes_fornecedores: "Caixa de e-mail (dado de terceiros)", n_fornecedores: "ERP · fornecedores", derivado: "Derivado (cálculo determinístico)",
};

export default function Investigations() {
  const { param } = useApp();
  return param ? <CaseDetail cid={param} /> : <CaseList />;
}

function CaseList() {
  const { iid, go } = useApp();
  const [filter, setFilter] = useState("");
  const { data, error } = useLive<any[]>(`/api/instances/${iid}/cases${filter ? `?state=${filter}` : ""}`, [filter]);
  return (
    <div className="space-y-4 rise">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Investigações</h1>
          <p className="text-sm text-slate-400">Fila priorizada por exposição × urgência do corte × força da evidência (heurística, não probabilidade).</p>
        </div>
        <label className="text-xs text-slate-400">Estado{" "}
          <select value={filter} onChange={(e) => setFilter(e.target.value)} className="ml-1 rounded-md border border-white/10 bg-ink-850 px-2 py-1 text-sm text-slate-200">
            <option value="">todos</option>
            {["investigating", "awaiting_approval", "escalated", "resolved", "failed"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>
      <ErrorBox error={error} />
      {!data ? <Spinner /> : data.length === 0 ? <Empty>Nenhum caso. Execute um ciclo do agente em Simulação.</Empty> : (
        <div className="overflow-hidden rounded-xl border border-white/5">
          <table className="w-full text-sm">
            <thead className="bg-ink-900 text-left text-[11px] uppercase tracking-wider text-slate-400">
              <tr><th className="px-3 py-2">Caso</th><th className="px-2">Regra</th><th className="px-2">Evidência</th><th className="px-2">Estado</th><th className="px-2 text-right">Exposição</th><th className="hidden px-3 lg:table-cell">Resumo</th></tr>
            </thead>
            <tbody className="divide-y divide-white/5 bg-ink-900/40">
              {data.map((c) => (
                <tr key={c.case_id} className="cursor-pointer hover:bg-white/[0.03]" onClick={() => go("cases", c.case_id)}>
                  <td className="px-3 py-2"><button className="font-mono text-xs text-sky-200 hover:underline" onClick={() => go("cases", c.case_id)}>{c.case_id}</button>
                    <div><Badge tone={sevTone[c.severity]}>{c.severity}</Badge></div></td>
                  <td className="px-2 text-xs">{ruleLabel(c.rule_id)}</td>
                  <td className="px-2"><Certainty certainty={c.certainty} strength={c.strength} /></td>
                  <td className="px-2"><State s={c.state} /></td>
                  <td className="num px-2 text-right text-xs">{brl(c.exposure_cents)}</td>
                  <td className="hidden max-w-[420px] truncate px-3 text-xs text-slate-400 lg:table-cell" title={c.summary}>{c.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CaseDetail({ cid }: { cid: string }) {
  const { iid, go } = useApp();
  const { data, error } = useLive<any>(`/api/instances/${iid}/cases/${encodeURIComponent(cid)}`, [cid]);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  const a = data.achado;
  return (
    <div className="space-y-4 rise">
      <button onClick={() => go("cases")} className="inline-flex items-center gap-1 text-sm text-brand-300 hover:underline"><ArrowLeft size={14} aria-hidden /> Investigações</button>
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-white/5 bg-ink-900/80 p-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono text-lg text-white">{data.caso.case_id}</h1>
            <State s={data.caso.state} />
            <Certainty certainty={a.certainty} strength={a.strength} />
            <Badge tone={sevTone[a.severity]}>{a.severity}</Badge>
          </div>
          <p className="mt-1 text-sm text-slate-300">{ruleLabel(a.rule_id)} — {a.summary}</p>
          {a.missing_data?.length > 0 && <p className="mt-1 text-xs text-amber-300">Dados faltantes: {a.missing_data.join("; ")}</p>}
        </div>
        <div className="text-right text-sm">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Exposição potencial</div>
          <div className="num text-xl font-semibold text-white">{brl(a.exposure_cents)}</div>
          <div className="text-[11px] text-slate-500">ação recomendada: {a.recommended_action}</div>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Títulos relacionados (estado operacional atual)" icon={<ListChecks size={16} aria-hidden />}>
          <div className="scroll-thin max-h-72 overflow-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-[11px] uppercase tracking-wider text-slate-400"><tr><th className="py-1">Título</th><th>Status</th><th>Lote</th><th className="text-right">Valor</th></tr></thead>
              <tbody className="divide-y divide-white/5">
                {data.titulos.map((t: any) => (
                  <tr key={t.titulo_id}>
                    <td className="py-1 font-mono">{t.titulo_id}</td>
                    <td><Badge tone={t.bloqueado ? "warn" : t.status === "pago" ? "ok" : t.status === "cancelado" ? "neutral" : "info"}>{t.bloqueado ? "bloqueado" : t.status}</Badge></td>
                    <td className="font-mono text-slate-400">{t.lote_remessa || "—"}</td>
                    <td className="num text-right">{brl(t.valor_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="Evidências por fonte" icon={<FileSearch size={16} aria-hidden />}>
          <div className="scroll-thin max-h-72 space-y-3 overflow-auto pr-1">
            {Object.entries(data.evidencias_por_fonte).map(([src, evs]: any) => (
              <div key={src}>
                <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  {SOURCES[src] || src}{src === "raw_comunicacoes_fornecedores" && <Badge tone="warn">não confiável</Badge>}
                </div>
                <ul className="space-y-0.5 text-xs">
                  {evs.map((e: any, i: number) => (
                    <li key={i} className="flex gap-2"><Mono>{e.ref}</Mono><span className="text-slate-400">{e.field}</span><span className="break-all text-slate-200">{e.value}</span>
                      {e.provenance !== "original" && <Badge tone="violet">{e.provenance}</Badge>}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Plano e decisões do agente" icon={<Scale size={16} aria-hidden />}>
          {data.decisoes.length === 0 ? <Empty>Sem decisões registradas.</Empty> : (
            <ol className="space-y-3">
              {data.decisoes.map((d: any) => (
                <li key={d.seq} className="rounded-lg bg-white/[0.02] p-3 ring-1 ring-inset ring-white/5">
                  <div className="flex flex-wrap items-center gap-2 text-xs"><span className="num font-mono text-slate-400">{ts(d.sim_ts)}</span><Badge tone="info">{d.decision}</Badge>
                    <Badge tone={d.llm.startsWith("ia:") ? "violet" : "neutral"} title="Participação real do LLM nesta decisão">{d.llm.startsWith("ia:") ? "LLM (validado)" : d.llm === "nao_usado" ? "determinístico" : "LLM indisponível → determinístico"}</Badge></div>
                  <p className="mt-1.5 text-xs leading-relaxed text-slate-300">{d.rationale}</p>
                  {d.actions?.length > 0 && <div className="mt-2 flex flex-wrap gap-1">{d.actions.map((x: any, i: number) => <Badge key={i} tone="neutral">{x.tool}{x.params?.titulo_id ? ` ${x.params.titulo_id}` : x.params?.pessoa_id ? ` → ${x.params.pessoa_id}` : ""}</Badge>)}</div>}
                  {d.policy_refs?.length > 0 && <div className="mt-1 text-[11px] text-slate-500">Política: {d.policy_refs.join(", ")}</div>}
                </li>
              ))}
            </ol>
          )}
        </Card>
        <Card title="Histórico da investigação" icon={<GitBranch size={16} aria-hidden />}>
          <ol className="relative space-y-2 border-l border-white/10 pl-4">
            {data.transicoes.map((t: any) => (
              <li key={t.seq} className="text-xs">
                <span className="absolute -left-[5px] mt-1 h-2.5 w-2.5 rounded-full bg-brand-500" aria-hidden />
                <div className="flex flex-wrap items-center gap-2"><span className="num font-mono text-slate-400">{ts(t.sim_ts)}</span>{t.from_state && <State s={t.from_state} />}→<State s={t.to_state} /><span className="text-slate-500">{t.actor}</span></div>
                <div className="mt-0.5 text-slate-300">{t.reason}</div>
              </li>
            ))}
          </ol>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Ferramentas executadas (auditoria)" icon={<ListChecks size={16} aria-hidden />}>
          {data.acoes.length === 0 ? <Empty>Nenhuma ação executada.</Empty> : (
            <table className="w-full text-xs">
              <thead className="text-left text-[11px] uppercase tracking-wider text-slate-400"><tr><th className="py-1">#</th><th>Quando</th><th>Ator</th><th>Ferramenta</th><th>Alvo</th><th>Resultado</th></tr></thead>
              <tbody className="divide-y divide-white/5">
                {data.acoes.map((x: any) => (
                  <tr key={x.seq}><td className="py-1 font-mono text-slate-500">{x.seq}</td><td className="num">{ts(x.sim_ts)}</td><td>{x.actor} <span className="text-slate-500">({x.actor_role})</span></td>
                    <td className="font-mono">{x.action}</td><td className="font-mono text-slate-400">{x.target}</td>
                    <td><Badge tone={x.decision === "executed" ? "ok" : x.decision === "requires_approval" ? "warn" : "danger"}>{x.decision}</Badge></td></tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Pendências e responsáveis" icon={<Users size={16} aria-hidden />} action={<button onClick={() => go("approvals")} className="text-xs text-brand-300 hover:underline">Caixa de aprovações</button>}>
          {data.aprovacoes.length === 0 ? <Empty>Sem solicitações de aprovação vinculadas.</Empty> : (
            <ul className="space-y-2 text-xs">
              {data.aprovacoes.map((s: any) => (
                <li key={s.solicitacao_id} className="rounded-lg bg-white/[0.02] p-2.5 ring-1 ring-inset ring-white/5">
                  <div className="flex flex-wrap items-center gap-2"><Stamp size={12} aria-hidden /><Mono>{s.solicitacao_id}</Mono><span className="font-mono">{s.tool}</span>
                    <Badge tone={s.status === "pending" ? "warn" : s.status === "rejected" ? "danger" : "ok"}>{s.status}</Badge></div>
                  <div className="mt-1 text-slate-400">Exige: {s.required.map((r: any) => r.pessoa_id || r.role).join(" + ")} · roteado para {s.routed_to.map((r: any) => r.candidatos[0] || "—").join(", ")}</div>
                  {s.approvals.map((ap: any, i: number) => (
                    <div key={i} className="mt-1 text-slate-300">✓ {ap.actor_id} ({ap.role}{ap.pessoa_id ? ` · ${ap.pessoa_id}` : ""}) {ts(ap.sim_ts)} {String(ap.comentario || "").startsWith("[SIMULAÇÃO DE TESTE]") && <Badge tone="demo">aprovação simulada</Badge>}</div>
                  ))}
                </li>
              ))}
            </ul>
          )}
          {data.pendencia && !["resolved", data.pendencia].includes(data.caso.state) && <div className="mt-2 text-xs text-slate-400">Próximo estado previsto: <State s={data.pendencia} /></div>}
        </Card>
      </div>

      {data.ia.length > 0 && (
        <Card title="Raciocínio contextual (LLM) — trilha validada" icon={<Bot size={16} aria-hidden />}>
          <ol className="space-y-1 text-xs">{data.ia.map((t: any) => <li key={t.seq}><Badge tone="violet">{t.kind}</Badge> <span className="text-slate-400">{t.validation}</span> <span className="break-all text-slate-300">{String(t.content).slice(0, 300)}</span></li>)}</ol>
        </Card>
      )}
    </div>
  );
}
