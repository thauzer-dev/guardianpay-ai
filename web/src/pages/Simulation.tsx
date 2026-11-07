import { FastForward, GitCompare, Pause, Play, RotateCcw, SkipForward, Sparkles, Waves } from "lucide-react";
import { useState } from "react";
import { api, brl, Instance, newKey, sessions, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Button, Card, Empty, ErrorBox, Mono, Ok, Spinner } from "../ui";

export default function Simulation({ instances }: { instances: Instance[] }) {
  const { iid, version } = useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [hours, setHours] = useState(24);
  const [speed, setSpeed] = useState(1.5);
  const ov = useLive<any>(`/api/instances/${iid}/overview`);
  const llm = useLive<any>(`/api/integrations/llm`);

  const cmd = (name: string, path: string, body?: unknown) => {
    if (busy) return;               // impede cliques duplicados enquanto há comando em voo
    setBusy(name); setErr(null); setMsg(null);
    api(`/api/instances/${iid}/sim/${path}`, { method: "POST", iid, idem: newKey(), body })
      .then((r: any) => setMsg(`${name}: ${r.replayed ? "repetição ignorada" : "ok"}${r.sim_now ? ` · relógio ${ts(r.sim_now)}` : ""}${r.passos !== undefined ? ` · ${r.passos} passo(s)` : ""}${r.aviso ? ` · ${r.aviso}` : ""}`))
      .catch(setErr)
      .finally(() => setBusy(null));
  };
  const checkLlm = () => { setBusy("llm"); api("/api/integrations/llm/check", { method: "POST" }).then(() => llm.reload()).catch(setErr).finally(() => setBusy(null)); };
  const ag = ov.data?.agente;

  return (
    <div className="space-y-4 rise">
      <div>
        <h1 className="text-xl font-semibold text-white">Controle de Simulação</h1>
        <p className="text-sm text-slate-400">Instância isolada <Mono>{iid}</Mono>. Comandos são serializados no servidor e protegidos por chave de idempotência. O agente continua rodando no servidor com a tela fechada.</p>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Relógio e execução" icon={<Waves size={16} aria-hidden />} className="xl:col-span-2">
          <div className="flex flex-wrap items-center gap-2">
            <Button tone="primary" busy={busy === "Passo"} onClick={() => cmd("Passo", "step")}><SkipForward size={14} aria-hidden /> Executar 1 passo</Button>
            <label className="flex items-center gap-1 text-xs text-slate-400">Avançar
              <input type="number" min={1} max={168} value={hours} onChange={(e) => setHours(Math.max(1, Math.min(168, +e.target.value || 1)))} className="w-16 rounded-md border border-white/10 bg-ink-850 px-2 py-1 text-sm text-slate-100" /> h</label>
            <Button busy={busy === "Avançar"} onClick={() => cmd("Avançar", "advance", { hours })}><FastForward size={14} aria-hidden /> Avançar</Button>
            {version?.executando
              ? <Button busy={busy === "Pausar"} onClick={() => cmd("Pausar", "pause")}><Pause size={14} aria-hidden /> Pausar</Button>
              : <Button busy={busy === "Continuar"} onClick={() => cmd("Continuar", "resume", { interval_s: speed })}><Play size={14} aria-hidden /> Executar continuamente</Button>}
            <label className="flex items-center gap-1 text-xs text-slate-400">1 h simulada a cada
              <select value={speed} onChange={(e) => setSpeed(+e.target.value)} className="rounded-md border border-white/10 bg-ink-850 px-1 py-1 text-sm text-slate-100">
                {[0.3, 0.8, 1.5, 3, 6].map((s) => <option key={s} value={s}>{s}s</option>)}
              </select></label>
            <Button tone="danger" busy={busy === "Reiniciar"} onClick={() => { if (confirm("Reiniciar esta instância? Todo o estado simulado será recriado a partir dos CSVs.")) { cmd("Reiniciar", "reset"); sessions.clear(iid); setTimeout(() => location.reload(), 600); } }}><RotateCcw size={14} aria-hidden /> Reiniciar instância</Button>
          </div>
          <div className="mt-3 grid gap-2 text-xs text-slate-400 sm:grid-cols-3">
            <div>Relógio: <span className="num font-mono text-slate-200">{ts(version?.sim_now)}</span> (tique {version?.tick}/168)</div>
            <div>Último ciclo: {ag?.ultimo_ciclo ? `${ag.ultimo_ciclo.acoes} ações · ${ag.ultimo_ciclo.casos_trabalhados} casos · lotes ${ag.ultimo_ciclo.lotes_fechados?.join(", ") || "—"}` : "—"}</div>
            <div>Lotes fecham 10h (Meridiano) e 14h (Litoral), só em dia útil.</div>
          </div>
          <div className="mt-3 space-y-2">{msg && <Ok>{msg}</Ok>}<ErrorBox error={err} /></div>
        </Card>
        <Card title="Simulador de aprovações (TESTE)" icon={<Sparkles size={16} aria-hidden />}>
          <p className="text-xs text-slate-400">Quando ligado, identidades de teste do cadastro respondem às solicitações segundo um cenário JSON versionado, só em expediente. Cada decisão é marcada <Badge tone="demo">[SIMULAÇÃO DE TESTE]</Badge> e registrada à parte. Não representa manifestação humana real.</p>
          <div className="mt-3 flex items-center gap-2">
            <Badge tone={ag?.simulador_aprovacao ? "demo" : "neutral"}>{ag?.simulador_aprovacao ? "ligado" : "desligado"}</Badge>
            <Button busy={busy === "Simulador"} onClick={() => cmd("Simulador", "approval-simulator", { enabled: !ag?.simulador_aprovacao })}>{ag?.simulador_aprovacao ? "Desligar" : "Ligar"}</Button>
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Integração AI Proxy (estado real)" icon={<Sparkles size={16} aria-hidden />}>
          {!llm.data ? <Spinner /> : (
            <div className="space-y-1.5 text-xs">
              <div>Chave configurada no servidor: <Badge tone={llm.data.configurado ? "ok" : "neutral"}>{llm.data.configurado ? "sim (valor nunca exibido)" : "não"}</Badge> · modo <Mono>{llm.data.modo}</Mono> · modelo <Mono>{llm.data.modelo_configurado || "não definido (lista /v1/models)"}</Mono></div>
              <div>Última verificação: {llm.data.ultima_verificacao.checked_at ? new Date(llm.data.ultima_verificacao.checked_at * 1000).toLocaleTimeString("pt-BR") : "nunca"}{" "}
                {llm.data.ultima_verificacao.ok === true && <Badge tone="ok">acessível · {llm.data.ultima_verificacao.models.length} modelos</Badge>}
                {llm.data.ultima_verificacao.ok === false && <Badge tone="danger">indisponível{llm.data.ultima_verificacao.tipo ? ` · ${llm.data.ultima_verificacao.tipo}` : ""}</Badge>}</div>
              {llm.data.ultima_verificacao.error && <div className="break-all text-rose-300">{llm.data.ultima_verificacao.error}</div>}
              {llm.data.ultima_verificacao.models?.length > 0 && <div className="text-slate-400">{llm.data.ultima_verificacao.models.join(", ")}</div>}
              <p className="text-slate-500">Sem o proxy, o agente opera no modo determinístico. Nenhuma resposta simulada é apresentada como inferência do LLM.</p>
              <Button busy={busy === "llm"} onClick={checkLlm}>{llm.data.modelo_configurado ? "Verificar agora (1 geração mínima)" : "Verificar agora (GET /v1/models)"}</Button>
            </div>
          )}
        </Card>
        <Compare instances={instances} />
        <EventsCard />
      </div>
    </div>
  );
}

function EventsCard() {
  const { iid } = useApp();
  const [after, setAfter] = useState(0);
  const { data, error } = useLive<any[]>(`/api/instances/${iid}/events?after=${after}&limit=40`, [after]);
  return (
    <Card title="Eventos persistidos" action={<div className="flex gap-1"><Button tone="ghost" onClick={() => setAfter(Math.max(0, after - 40))} disabled={after === 0}>‹</Button><Button tone="ghost" onClick={() => setAfter(after + 40)}>›</Button></div>}>
      <ErrorBox error={error} />
      {!data ? <Spinner /> : data.length === 0 ? <Empty>Sem eventos nesta faixa.</Empty> : (
        <ul className="scroll-thin max-h-72 space-y-1 overflow-auto text-[11px]">
          {data.map((e) => (
            <li key={e.seq} className="flex flex-wrap items-center gap-1.5"><span className="font-mono text-slate-500">#{e.seq}</span><span className="num font-mono text-slate-400">{ts(e.sim_ts)}</span>
              <Badge tone={e.trusted ? "info" : "warn"}>{e.type}</Badge><span className="font-mono text-slate-300">{e.entity_id}</span>
              <Badge tone={e.processed === 1 ? "ok" : e.processed === 2 ? "danger" : "neutral"}>{e.processed === 1 ? "processado" : e.processed === 2 ? "dead-letter" : "pendente"}</Badge></li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Compare({ instances }: { instances: Instance[] }) {
  const { iid } = useApp();
  const others = instances.filter((i) => i.id !== iid);
  const [b, setB] = useState(others[0]?.id || "");
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const run = () => {
    const tb = sessions.get(b);
    if (!tb) { setErr(new Error("Faça login também na instância B (sessões são isoladas por instância).")); return; }
    setErr(null);
    api(`/api/compare?a=${iid}&b=${b}`, { iid, headers: { "X-Compare-Token": tb.token } }).then(setData).catch(setErr);
  };
  const rows: [string, string][] = [["perda_simulada_cents", "Perda simulada"], ["economia_estimada_realizada_cents", "Economia estimada"], ["economia_estimada_projetada_cents", "Economia projetada"], ["custo_atraso_estimado_cents", "Custo de atraso"], ["placar_cents", "Placar"]];
  return (
    <Card title="Comparar cenários" icon={<GitCompare size={16} aria-hidden />}>
      {others.length === 0 ? <Empty>Crie outra instância (por exemplo, sem simulador de aprovação) para comparar.</Empty> : (
        <>
          <div className="flex items-center gap-2 text-xs">
            <span className="text-slate-400">com</span>
            <select value={b} onChange={(e) => setB(e.target.value)} className="rounded-md border border-white/10 bg-ink-850 px-2 py-1 text-slate-100">{others.map((o) => <option key={o.id} value={o.id}>{o.nome}</option>)}</select>
            <Button onClick={run}>Comparar</Button>
          </div>
          <ErrorBox error={err} />
          {data && (
            <table className="mt-3 w-full text-xs">
              <thead className="text-left text-slate-400"><tr><th></th>{Object.values<any>(data).map((d) => <th key={d.nome} className="text-right">{d.nome}<div className="font-normal text-slate-500">{ts(d.sim_now)}{d.simulador_aprovacao ? " · sim. teste" : ""}</div></th>)}</tr></thead>
              <tbody className="divide-y divide-white/5">{rows.map(([k, l]) => <tr key={k}><td className="py-1 text-slate-300">{l}</td>{Object.values<any>(data).map((d, i) => <td key={i} className="num text-right">{brl(d.indicadores[k])}</td>)}</tr>)}</tbody>
            </table>
          )}
        </>
      )}
    </Card>
  );
}
