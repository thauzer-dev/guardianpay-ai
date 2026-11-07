import { Activity, Banknote, Boxes, ClipboardList, Landmark, ShieldCheck, Stamp } from "lucide-react";
import { brl, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Card, Empty, ErrorBox, Spinner, Stat, State, sevTone, stateLabel } from "../ui";

type OV = {
  agente: { executando: boolean; sim_now: string; tick: number; fim: boolean; llm: string; llm_motivo?: string | null; simulador_aprovacao: boolean; auditoria_integra: boolean; ultimo_ciclo: any; erro: string | null };
  casos_por_estado: Record<string, number>;
  casos_por_severidade: Record<string, number>;
  aprovacoes_pendentes: number;
  acoes_executadas: number;
  lotes: any[];
  indicadores: any;
  timeline: any[];
  proximo_corte: { lote_remessa: string; fecha_em: string; sairao: number; retidos: number }[];
  ferramentas: Record<string, number>;
};

export default function Overview() {
  const { iid, go } = useApp();
  const { data, error } = useLive<OV>(`/api/instances/${iid}/overview`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  const ind = data.indicadores;
  const totalCases = Object.values(data.casos_por_estado).reduce((a, b) => a + b, 0);
  const pending = (data.casos_por_estado.awaiting_approval || 0) + (data.casos_por_estado.investigating || 0) + (data.casos_por_estado.escalated || 0);

  return (
    <div className="space-y-5 rise">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Centro de Operações</h1>
          <p className="text-sm text-slate-400">O agente roda no servidor; esta tela apenas observa. Valores monetários de estimativa estão marcados como tal.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge tone={data.agente.executando ? "ok" : "neutral"}>{data.agente.executando ? "Executando" : data.agente.fim ? "Período encerrado" : "Pausado"}</Badge>
          <Badge tone={data.agente.llm === "habilitado" ? "ok" : "warn"} title={data.agente.llm_motivo || "Estado real da integração AI Proxy"}>LLM: {data.agente.llm}</Badge>
          {data.agente.simulador_aprovacao && <Badge tone="demo" title="Decisões de aprovação programadas de teste">Simulador de aprovação (TESTE)</Badge>}
          <Badge tone={data.agente.auditoria_integra ? "ok" : "danger"}><ShieldCheck size={12} aria-hidden /> Auditoria {data.agente.auditoria_integra ? "íntegra" : "violada"}</Badge>
        </div>
      </div>

      <section aria-label="Estado atual do agente" className="grid gap-3 rounded-xl border border-brand-500/20 bg-gradient-to-r from-brand-500/[0.07] to-transparent p-4 lg:grid-cols-[1.2fr_1fr_1.4fr]">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Agora</div>
          <div className="mt-1 flex items-center gap-2 text-lg font-semibold text-white">
            <span className={`h-2.5 w-2.5 rounded-full ${data.agente.executando ? "bg-emerald-400 pulse-dot" : data.agente.erro ? "bg-rose-400" : "bg-slate-500"}`} aria-hidden />
            {data.agente.erro ? "Execução interrompida por erro" : data.agente.executando ? "Operando no servidor" : data.agente.fim ? "Período simulado encerrado" : "Aguardando comando"}
          </div>
          <div className="mt-1 text-xs text-slate-400">
            {data.agente.ultimo_ciclo ? <>Último ciclo ({ts(data.agente.ultimo_ciclo.sim_now)}): {data.agente.ultimo_ciclo.acoes} chamada(s) de ferramenta (incl. solicitações), {data.agente.ultimo_ciclo.casos_trabalhados} caso(s) trabalhados{data.agente.ultimo_ciclo.lotes_fechados?.length ? `, lote ${data.agente.ultimo_ciclo.lotes_fechados.join(", ")} enviado` : ""}.</> : "Nenhum ciclo executado nesta sessão do servidor."}
          </div>
          {data.agente.erro && <div className="mt-1 text-xs text-rose-300">{data.agente.erro}</div>}
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Próximo corte bancário</div>
          {data.proximo_corte[0] ? (
            <>
              <div className="mt-1 font-mono text-sm text-white">{data.proximo_corte[0].lote_remessa}</div>
              <div className="text-xs text-slate-400">fecha {ts(data.proximo_corte[0].fecha_em)} · em {Math.max(0, Math.round((new Date(data.proximo_corte[0].fecha_em).getTime() - new Date(data.agente.sim_now).getTime()) / 3.6e6))} h simuladas</div>
              <div className="mt-1 text-xs"><span className="text-slate-200">{data.proximo_corte[0].sairao}</span> sairão · <span className="text-amber-300">{data.proximo_corte[0].retidos}</span> retidos pelo agente</div>
            </>
          ) : <div className="mt-1 text-sm text-slate-400">Nenhum lote aberto.</div>}
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Ferramentas usadas pelo agente (auditadas)</div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {Object.keys(data.ferramentas).length === 0 ? <span className="text-xs text-slate-500">nenhuma ainda</span> :
              Object.entries(data.ferramentas).map(([k, n]) => <Badge key={k} tone="info">{k} <span className="num">×{n}</span></Badge>)}
          </div>
          <button onClick={() => go("journey")} className="mt-2 text-xs text-brand-300 hover:underline">Acompanhar a jornada guiada →</button>
        </div>
      </section>

      {data.agente.tick === 0 && !data.agente.ultimo_ciclo && totalCases === 0 && (
        <Empty>Nenhum ciclo executado ainda. Vá em <button className="text-brand-300 underline" onClick={() => go("sim")}>Simulação</button> e execute um passo ou inicie o agente.</Empty>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Casos detectados" value={totalCases} hint={`${pending} em andamento · ${data.casos_por_estado.resolved || 0} resolvidos`} />
        <Stat label="Ações executadas" value={data.acoes_executadas} hint="via executor + motor de políticas" />
        <Stat label="Aprovações pendentes" value={data.aprovacoes_pendentes} tone={data.aprovacoes_pendentes ? "warn" : undefined} hint={<button className="text-brand-300 hover:underline" onClick={() => go("approvals")}>abrir caixa</button>} />
        <Stat label="Pagamentos processados" value={ind.pagamentos_processados.titulos} hint={`${brl(ind.pagamentos_processados.valor_cents)} em lotes fechados (simulação)`} />
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Indicadores financeiros" icon={<Banknote size={16} aria-hidden />} action={<span className="text-[11px] text-slate-500">sem gabarito · estimativas sem validação estatística</span>}>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Fatos da simulação</div>
          <div className="grid gap-2 sm:grid-cols-3">
            <Stat label="Retenções executadas" value={ind.retencoes_executadas.titulos} hint={`${ind.retencoes_executadas.em_lotes_fechados} em lotes já fechados`} />
            <Stat label="Perda simulada" value={brl(ind.perda_simulada_cents)} tone={ind.perda_simulada_cents ? "danger" : "ok"} hint="comprovado e pago sem saneamento" />
            <Stat label="Saneado antes do pagamento" value={brl(ind.saneado_antes_do_pagamento_cents)} hint="correções aprovadas" />
          </div>
          <div className="mb-2 mt-4 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Estimativas <span className="normal-case tracking-normal text-slate-500">— "indevido" = achado comprovado pelo motor; não há gabarito externo</span></div>
          <div className="grid gap-2 sm:grid-cols-4">
            <Stat label="Exposição potencial" value={brl(ind.exposicao_potencial_cents)} hint="achados comprovados, 1 por título" />
            <Stat label="Evitado estimado (lotes enviados)" value={brl(ind.economia_estimada_realizada_cents)} tone="ok" hint="estimativa da simulação sobre dados fictícios — não é economia comprovada" />
            <Stat label="Evitável (lotes futuros)" value={brl(ind.economia_estimada_projetada_cents)} hint="projeção estimada: depende do estado em 09/11 08h" />
            <Stat label="Custo de atraso" value={brl(ind.custo_atraso_estimado_cents)} tone="warn" hint="títulos legítimos retidos" />
          </div>
          <div className="mt-3 text-[11px] text-slate-500">Placar (parametros.md): <span className="num font-mono text-slate-300">{brl(ind.placar_cents)}</span> · acionamentos: {ind.acionamentos} ({ind.acionamentos_sem_caso_comprovado} sem caso comprovado) · indícios não verificáveis: {brl(ind.nao_verificavel_indicios_cents)}</div>
        </Card>

        <Card title="Casos" icon={<ClipboardList size={16} aria-hidden />}>
          <ul className="space-y-1.5 text-sm">
            {Object.entries(data.casos_por_estado).map(([s, n]) => (
              <li key={s} className="flex items-center justify-between">
                <State s={s} />
                <span className="num font-medium">{n}</span>
              </li>
            ))}
          </ul>
          <div className="mt-4 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Em aberto por severidade</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(data.casos_por_severidade).length === 0 ? <span className="text-xs text-slate-500">nenhum</span> :
              Object.entries(data.casos_por_severidade).map(([s, n]) => <Badge key={s} tone={sevTone[s]}>{s}: {n}</Badge>)}
          </div>
          <button onClick={() => go("cases")} className="mt-4 text-sm text-brand-300 hover:underline">Ver investigações →</button>
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-5">
        <Card className="xl:col-span-3" title="Lotes de remessa" icon={<Landmark size={16} aria-hidden />}>
          <div className="scroll-thin max-h-[420px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-ink-900 text-left text-[11px] uppercase tracking-wider text-slate-400">
                <tr><th className="py-1.5 pr-2">Lote</th><th className="pr-2">Fechamento</th><th className="pr-2">Situação</th><th className="pr-2 text-right">Na fila</th><th className="pr-2 text-right">Bloq.</th><th className="text-right">Enviado</th></tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {data.lotes.map((l) => (
                  <tr key={l.lote_remessa}>
                    <td className="py-1.5 pr-2 font-mono text-xs">{l.lote_remessa}</td>
                    <td className="pr-2 text-xs text-slate-400">{l.data_prevista.slice(8, 10)}/{l.data_prevista.slice(5, 7)} {l.horario_fechamento}</td>
                    <td className="pr-2"><Badge tone={l.status === "enviado" ? "ok" : "info"}>{l.status === "enviado" ? "enviado" : "aberto"}</Badge></td>
                    <td className="num pr-2 text-right text-xs">{l.titulos} · {brl(l.valor_cents)}</td>
                    <td className="num pr-2 text-right text-xs">{l.bloqueados ? <span className="text-amber-300">{l.bloqueados}</span> : "—"}</td>
                    <td className="num text-right text-xs">{l.titulos_enviados !== null ? `${l.titulos_enviados} · ${brl(l.valor_enviado_cents)}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card className="xl:col-span-2" title="Linha do tempo operacional" icon={<Activity size={16} aria-hidden />}>
          {data.timeline.length === 0 ? <Empty>Sem atividade ainda.</Empty> : (
            <ol className="scroll-thin max-h-[420px] space-y-2 overflow-auto pr-1" aria-live="polite">
              {data.timeline.map((e, i) => (
                <li key={`${e.tipo}-${e.ord}-${i}`} className="rounded-lg bg-white/[0.02] px-2.5 py-1.5 ring-1 ring-inset ring-white/5">
                  <div className="flex items-center gap-2 text-[11px] text-slate-400">
                    <span className="num font-mono">{ts(e.sim_ts)}</span>
                    {e.tipo === "caso" ? <Boxes size={12} aria-hidden /> : <Stamp size={12} aria-hidden />}
                    <span className="truncate">{e.ator}</span>
                  </div>
                  <div className="text-xs">
                    {e.tipo === "caso" ? (
                      <button className="text-left hover:underline" onClick={() => go("cases", e.ref)}>
                        <span className="font-mono text-sky-200">{e.ref}</span> {e.titulo.split(" → ").map((x: string) => stateLabel[x] || x || "novo").join(" → ")}
                      </button>
                    ) : (
                      <span><span className="font-medium text-slate-200">{e.titulo}</span> {e.ref && <span className="font-mono text-sky-200">{e.ref}</span>} <Badge tone={e.detalhe === "executed" ? "ok" : e.detalhe === "requires_approval" ? "warn" : "danger"}>{e.detalhe}</Badge></span>
                    )}
                  </div>
                  {e.tipo === "caso" && <div className="line-clamp-2 text-[11px] text-slate-500">{e.detalhe}</div>}
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>
    </div>
  );
}
