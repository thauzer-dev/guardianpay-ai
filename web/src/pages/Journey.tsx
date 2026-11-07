import { CheckCircle2, CircleDashed, Clock3, ExternalLink, FastForward, SkipForward, UserRoundCog } from "lucide-react";
import { useState } from "react";
import { api, newKey, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Button, Card, ErrorBox, Mono, Spinner } from "../ui";

type Step = { id: string; titulo: string; status: "ok" | "parcial" | "pendente"; detalhe: string; evidencias: string[]; proximo: string | null };
type Scenario = { id: string; nome: string; caso: string; titulo: string; etapas: Step[]; aprovador_sugerido?: string };

const ICON = {
  ok: <CheckCircle2 size={18} className="text-emerald-400" aria-label="concluída" />,
  parcial: <Clock3 size={18} className="text-amber-300" aria-label="parcial" />,
  pendente: <CircleDashed size={18} className="text-slate-500" aria-label="pendente" />,
};

export default function Journey() {
  const { iid, go, user, switchUser } = useApp();
  const { data, error } = useLive<{ sim_now: string; horas_ate_corte_a: number; cenarios: Scenario[]; aviso: string }>(`/api/instances/${iid}/demo/journey`);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const run = (name: string, path: string, body?: unknown) => {
    if (busy) return;
    setBusy(name); setErr(null);
    api(`/api/instances/${iid}/sim/${path}`, { method: "POST", iid, idem: newKey(), body }).catch(setErr).finally(() => setBusy(null));
  };
  const become = (login: string) => { setBusy("user"); switchUser(login).catch(setErr).finally(() => setBusy(null)); };
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  const done = data.cenarios.flatMap((c) => c.etapas).filter((s) => s.status === "ok").length;

  return (
    <div className="space-y-4 rise">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Jornada guiada</h1>
          <p className="text-sm text-slate-400">{data.aviso} Relógio simulado <span className="num font-mono text-slate-200">{ts(data.sim_now)}</span> · {done}/10 etapas verificadas.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button tone="primary" busy={busy === "step"} onClick={() => run("step", "step")}><SkipForward size={14} aria-hidden /> Executar 1 passo do agente</Button>
          {data.horas_ate_corte_a > 0 && (
            <Button busy={busy === "adv"} onClick={() => run("adv", "advance", { hours: data.horas_ate_corte_a })}><FastForward size={14} aria-hidden /> Avançar até o corte de 03/11 10:00 ({data.horas_ate_corte_a} h)</Button>
          )}
        </div>
      </div>
      <ErrorBox error={err} />
      <div className="grid gap-4 xl:grid-cols-2">
        {data.cenarios.map((c) => (
          <Card key={c.id} title={<span>Cenário {c.id} — {c.nome}</span>} action={<button onClick={() => go("cases", c.caso)} className="inline-flex items-center gap-1 text-xs text-brand-300 hover:underline">{c.caso}<ExternalLink size={12} aria-hidden /></button>}>
            <ol className="space-y-3">
              {c.etapas.map((s, i) => (
                <li key={s.id} className={`flex gap-3 rounded-lg p-2.5 ring-1 ring-inset ${s.status === "ok" ? "bg-emerald-500/[0.04] ring-emerald-500/15" : s.status === "parcial" ? "bg-amber-500/[0.04] ring-amber-500/15" : "ring-white/5"}`}>
                  <div className="mt-0.5 shrink-0">{ICON[s.status]}</div>
                  <div className="min-w-0 text-sm">
                    <div className="font-medium text-slate-100">{i + 1}. {s.titulo}</div>
                    <div className="mt-0.5 text-xs leading-relaxed text-slate-300">{s.detalhe}</div>
                    {s.evidencias.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{s.evidencias.map((e) => <Mono key={e}>{e}</Mono>)}</div>}
                    {s.proximo && <div className="mt-1 text-xs text-amber-200">Próximo: {s.proximo}</div>}
                  </div>
                </li>
              ))}
            </ol>
            {c.id === "B" && c.aprovador_sugerido && (
              <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/5 pt-3 text-xs text-slate-400">
                <UserRoundCog size={14} aria-hidden /> Você está como <strong className="text-slate-200">{user.nome}</strong> ({user.papel}).
                {user.login !== c.aprovador_sugerido ? (
                  <Button busy={busy === "user"} onClick={() => become(c.aprovador_sugerido!)}>Entrar como {c.aprovador_sugerido} (login DEMO)</Button>
                ) : (
                  <Button tone="primary" onClick={() => go("approvals")}>Abrir caixa de aprovações</Button>
                )}
                <Badge tone="demo">troca de identidade só no modo demonstração</Badge>
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
