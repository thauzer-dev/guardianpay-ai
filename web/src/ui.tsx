import { AlertTriangle, CheckCircle2, CircleDashed, Info, Loader2, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";

export function Card({ title, icon, children, className = "", action }: { title?: ReactNode; icon?: ReactNode; children: ReactNode; className?: string; action?: ReactNode }) {
  return (
    <section className={`rounded-xl border border-white/5 bg-ink-900/80 shadow-card ${className}`}>
      {title && (
        <header className="flex items-center justify-between gap-2 border-b border-white/5 px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold tracking-wide text-slate-200">
            {icon}
            {title}
          </h2>
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const tones: Record<string, string> = {
  neutral: "bg-slate-500/10 text-slate-300 ring-slate-500/20",
  info: "bg-sky-500/10 text-sky-300 ring-sky-500/25",
  ok: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/25",
  warn: "bg-amber-500/10 text-amber-300 ring-amber-500/25",
  danger: "bg-rose-500/10 text-rose-300 ring-rose-500/25",
  violet: "bg-violet-500/10 text-violet-300 ring-violet-500/25",
  demo: "bg-fuchsia-500/15 text-fuchsia-200 ring-fuchsia-400/40",
};

export function Badge({ tone = "neutral", children, title }: { tone?: keyof typeof tones | string; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${tones[tone] || tones.neutral}`}>
      {children}
    </span>
  );
}

export const stateTone: Record<string, string> = {
  detected: "info", investigating: "info", awaiting_approval: "warn", authorized: "violet", executing: "violet",
  resolved: "ok", escalated: "danger", failed: "danger",
};
export const stateLabel: Record<string, string> = {
  detected: "Detectado", investigating: "Investigando", awaiting_approval: "Aguardando aprovação", authorized: "Autorizado",
  executing: "Executando", resolved: "Resolvido", escalated: "Escalado", failed: "Falhou",
};
export const sevTone: Record<string, string> = { critica: "danger", alta: "warn", media: "info", baixa: "neutral" };

/** Distingue visualmente confirmação (comprovado), evidência forte e suspeita (indício). */
export function Certainty({ certainty, strength }: { certainty: string; strength?: string }) {
  if (certainty === "comprovado")
    return <Badge tone="danger" title="Irregularidade demonstrada por registros"><ShieldAlert size={12} aria-hidden /> Confirmado</Badge>;
  if (strength === "forte") return <Badge tone="warn" title="Indício com evidência forte"><AlertTriangle size={12} aria-hidden /> Evidência forte</Badge>;
  return <Badge tone="neutral" title="Suspeita: exige investigação"><CircleDashed size={12} aria-hidden /> Suspeita</Badge>;
}

export function State({ s }: { s: string }) {
  return <Badge tone={stateTone[s] || "neutral"}>{stateLabel[s] || s}</Badge>;
}

export function Spinner({ label = "Carregando" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 py-6 text-sm text-slate-400">
      <Loader2 className="motion-safe:animate-spin" size={16} aria-hidden /> {label}…
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="flex items-center gap-2 rounded-lg border border-dashed border-white/10 px-4 py-6 text-sm text-slate-400"><Info size={16} aria-hidden />{children}</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden /> {String((error as Error)?.message || error)}
    </div>
  );
}

export function Ok({ children }: { children: ReactNode }) {
  return <div role="status" className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200"><CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden />{children}</div>;
}

export function Button({ children, onClick, disabled, tone = "default", type = "button", title, busy }: {
  children: ReactNode; onClick?: () => void; disabled?: boolean; tone?: "default" | "primary" | "danger" | "ghost"; type?: "button" | "submit"; title?: string; busy?: boolean;
}) {
  const t = {
    default: "bg-white/5 hover:bg-white/10 text-slate-100 ring-1 ring-inset ring-white/10",
    primary: "bg-brand-500 hover:bg-brand-400 text-ink-950 font-semibold",
    danger: "bg-rose-500/90 hover:bg-rose-500 text-white font-semibold",
    ghost: "hover:bg-white/5 text-slate-300",
  }[tone];
  return (
    <button type={type} title={title} onClick={onClick} disabled={disabled || busy} aria-busy={busy}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${t}`}>
      {busy && <Loader2 size={14} className="motion-safe:animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: string }) {
  const c = tone === "ok" ? "text-emerald-300" : tone === "danger" ? "text-rose-300" : tone === "warn" ? "text-amber-300" : "text-slate-50";
  return (
    <div className="rounded-lg bg-white/[0.03] px-3 py-2.5 ring-1 ring-inset ring-white/5">
      <div className="text-[11px] uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`num mt-1 text-lg font-semibold ${c}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-slate-500">{hint}</div>}
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded bg-white/5 px-1 py-0.5 font-mono text-[12px] text-sky-200">{children}</code>;
}
