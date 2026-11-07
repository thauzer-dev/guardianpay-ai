import { Bell, Mail, MessageSquare, Stamp } from "lucide-react";
import { useState } from "react";
import { api, ts } from "../api";
import { useApp } from "../App";
import { useLive } from "../hooks";
import { Badge, Button, Empty, ErrorBox, Mono, Spinner } from "../ui";

const KIND: Record<string, { label: string; icon: JSX.Element; tone: string }> = {
  acionamento: { label: "Acionamento", icon: <Bell size={14} aria-hidden />, tone: "warn" },
  aprovacao: { label: "Aprovação", icon: <Stamp size={14} aria-hidden />, tone: "info" },
  fornecedor: { label: "Fornecedor (canal do cadastro)", icon: <Mail size={14} aria-hidden />, tone: "violet" },
};

export default function Notifications() {
  const { iid } = useApp();
  const [mine, setMine] = useState(true);
  const { data, error, reload } = useLive<any[]>(`/api/instances/${iid}/notifications?mine=${mine}`, [mine]);
  const read = (id: string) => api(`/api/instances/${iid}/notifications/${id}/read`, { method: "POST", iid }).then(reload);
  return (
    <div className="space-y-4 rise">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Notificações</h1>
          <p className="text-sm text-slate-400">Canal interno (outbox). Envio externo é <strong>simulado</strong>: adaptadores de Slack/Telegram/e-mail ficam desacoplados e nenhuma mensagem externa vale como autorização financeira.</p>
        </div>
        <div className="flex gap-1 rounded-lg bg-white/5 p-1 text-xs">
          <button onClick={() => setMine(true)} className={`rounded px-2.5 py-1 ${mine ? "bg-brand-500/20 text-brand-200" : "text-slate-300"}`}>Para mim</button>
          <button onClick={() => setMine(false)} className={`rounded px-2.5 py-1 ${!mine ? "bg-brand-500/20 text-brand-200" : "text-slate-300"}`}>Todas</button>
        </div>
      </div>
      <ErrorBox error={error} />
      {!data ? <Spinner /> : data.length === 0 ? <Empty>Nenhuma notificação{mine ? " para você" : ""}.</Empty> : (
        <ul className="space-y-2">
          {data.map((n) => {
            const k = KIND[n.kind] || { label: n.kind, icon: <MessageSquare size={14} aria-hidden />, tone: "neutral" };
            return (
              <li key={n.msg_id} className={`rounded-xl border border-white/5 bg-ink-900/70 p-3 ${n.lida ? "opacity-60" : ""}`}>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge tone={k.tone}>{k.icon}{k.label}</Badge><span className="text-slate-400">para</span><Mono>{n.to_ref}</Mono>
                  <span className="num font-mono text-slate-500">{ts(n.sim_ts)}</span><Badge tone="neutral">envio simulado</Badge>
                  {!n.lida && <Button tone="ghost" onClick={() => read(n.msg_id)}>marcar como lida</Button>}
                </div>
                <p className="mt-1.5 text-sm text-slate-200">{n.body}</p>
                {n.titulos?.length > 0 && <div className="mt-1 text-[11px] text-slate-500">Títulos: {n.titulos.slice(0, 8).join(", ")}{n.titulos.length > 8 ? "…" : ""}</div>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
