import { Bell, Bot, Cpu, FlaskConical, Gauge, KeyRound, LogOut, Plus, Route, Search, ShieldCheck, Stamp } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError, hasOperator, Instance, sessions, setOperator, ts, User, Version } from "./api";
import { Badge, Button, Card, ErrorBox, Spinner } from "./ui";
import Overview from "./pages/Overview";
import Investigations from "./pages/Investigations";
import Approvals from "./pages/Approvals";
import AgentWorkspace from "./pages/AgentWorkspace";
import Simulation from "./pages/Simulation";
import Notifications from "./pages/Notifications";
import Journey from "./pages/Journey";

type Page = "overview" | "journey" | "cases" | "approvals" | "agent" | "sim" | "notifications";
type Ctx = { iid: string; user: User; version: Version | null; go: (p: Page, param?: string) => void; param?: string; logout: () => void;
  switchUser: (login: string) => Promise<void> };
export const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

const NAV: { id: Page; label: string; icon: JSX.Element }[] = [
  { id: "overview", label: "Centro de Operações", icon: <Gauge size={16} aria-hidden /> },
  { id: "journey", label: "Jornada guiada", icon: <Route size={16} aria-hidden /> },
  { id: "cases", label: "Investigações", icon: <Search size={16} aria-hidden /> },
  { id: "approvals", label: "Aprovações", icon: <Stamp size={16} aria-hidden /> },
  { id: "agent", label: "Agente", icon: <Bot size={16} aria-hidden /> },
  { id: "sim", label: "Simulação", icon: <FlaskConical size={16} aria-hidden /> },
  { id: "notifications", label: "Notificações", icon: <Bell size={16} aria-hidden /> },
];

function readHash(): { iid?: string; page: Page; param?: string } {
  const [iid, page, param] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { iid: iid || undefined, page: (NAV.some((n) => n.id === page) ? page : "overview") as Page, param: param ? decodeURIComponent(param) : undefined };
}

export default function App() {
  const [route, setRoute] = useState(readHash());
  const [instances, setInstances] = useState<Instance[] | null>(null);
  const [health, setHealth] = useState<{ demo_mode: boolean } | null>(null);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    const on = () => setRoute(readHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const [needOp, setNeedOp] = useState(false);
  const loadInstances = useCallback(() => {
    api<Instance[]>("/api/instances").then((x) => { setInstances(x); setNeedOp(false); })
      .catch((e) => (e instanceof ApiError && (e.status === 401 || e.status === 403) ? setNeedOp(true) : setErr(e)));
  }, []);
  useEffect(() => {
    api<{ demo_mode: boolean }>("/api/health")
      .then(async (h) => {
        setHealth(h);
        if (h.demo_mode && !hasOperator()) {
          // mecanismo LOCAL de demonstração: o servidor só entrega o token a clientes loopback
          await api<{ token: string }>("/api/demo/operator").then((r) => setOperator(r.token)).catch(() => undefined);
        }
        loadInstances();
      })
      .catch(setErr);
  }, [loadInstances]);

  const nav = (iid: string, page: Page = "overview", param?: string) => {
    window.location.hash = `/${iid}/${page}${param ? "/" + encodeURIComponent(param) : ""}`;
  };

  if (err) return <div className="p-8"><ErrorBox error={err} /></div>;
  if (needOp) return <OperatorGate onOk={loadInstances} demo={!!health?.demo_mode} />;
  if (!instances || !health) return <div className="p-8"><Spinner /></div>;
  if (!route.iid || !instances.some((i) => i.id === route.iid))
    return <InstancePicker instances={instances} demo={health.demo_mode} onPick={(id) => nav(id)} onCreated={loadInstances} />;
  return <Shell key={route.iid} iid={route.iid} instances={instances} page={route.page} param={route.param} nav={nav} demo={health.demo_mode} />;
}

function Shell({ iid, instances, page, param, nav, demo }: { iid: string; instances: Instance[]; page: Page; param?: string; nav: (i: string, p?: Page, x?: string) => void; demo: boolean }) {
  const [sess, setSess] = useState<{ token: string; user: User } | undefined>(sessions.get(iid));
  const [version, setVersion] = useState<Version | null>(null);
  const inst = instances.find((i) => i.id === iid)!;

  useEffect(() => {
    if (!sess) return;
    let alive = true;
    const tick = () =>
      api<Version>(`/api/instances/${iid}/version`, { iid })
        .then((v) => alive && setVersion((old) => (old && JSON.stringify(old) === JSON.stringify(v) ? old : v)))
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) {
            sessions.clear(iid);
            setSess(undefined);
          }
        });
    tick();
    const h = setInterval(tick, 1500);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [iid, sess]);

  if (!sess) return <DemoLogin iid={iid} demo={demo} onLogged={() => setSess(sessions.get(iid))} />;
  const logout = () => {
    api(`/api/instances/${iid}/auth/logout`, { method: "POST", iid }).catch(() => undefined);
    sessions.clear(iid);
    setSess(undefined);
  };
  const switchUser = async (login: string) => {
    api(`/api/instances/${iid}/auth/logout`, { method: "POST", iid }).catch(() => undefined);
    const r = await api<{ token: string; usuario: User }>(`/api/instances/${iid}/auth/demo-login`, { method: "POST", body: { login } });
    sessions.set(iid, r.token, r.usuario);
    setSess(sessions.get(iid));
  };
  const ctx: Ctx = { iid, user: sess.user, version, go: (p, x) => nav(iid, p, x), param, logout, switchUser };
  const progress = version ? Math.round((version.tick / 168) * 100) : 0;

  return (
    <AppCtx.Provider value={ctx}>
      <div className="flex min-h-screen">
        <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-brand-500 focus:px-3 focus:py-1 focus:text-ink-950">Pular para o conteúdo</a>
        <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-white/5 bg-ink-900/60 px-3 py-4 md:flex">
          <div className="mb-6 flex items-center gap-2 px-2">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-brand-400 to-indigo-500 text-ink-950"><ShieldCheck size={18} aria-hidden /></div>
            <div>
              <div className="text-sm font-semibold text-white">GuardianPay AI</div>
              <div className="text-[11px] text-slate-400">Autonomous AP Operations</div>
            </div>
          </div>
          <nav aria-label="Módulos" className="flex flex-col gap-0.5">
            {NAV.map((n) => (
              <button key={n.id} onClick={() => nav(iid, n.id)} aria-current={page === n.id ? "page" : undefined}
                className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${page === n.id ? "bg-brand-500/10 text-brand-300 ring-1 ring-inset ring-brand-500/20" : "text-slate-300 hover:bg-white/5"}`}>
                {n.icon}
                {n.label}
              </button>
            ))}
          </nav>
          <div className="mt-auto space-y-2 px-2 text-[11px] text-slate-500">
            <div>Instância isolada</div>
            <select aria-label="Trocar instância" value={iid} onChange={(e) => nav(e.target.value)} className="w-full rounded-md border border-white/10 bg-ink-850 px-2 py-1 text-xs text-slate-200">
              {instances.map((i) => <option key={i.id} value={i.id}>{i.nome}</option>)}
            </select>
            <button onClick={() => (window.location.hash = "")} className="text-brand-300 hover:underline">Gerenciar instâncias</button>
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b border-white/5 bg-ink-950/85 px-4 py-2.5 backdrop-blur">
            <div className="flex items-center gap-2 text-sm">
              <span className={`h-2 w-2 rounded-full ${version?.executando ? "bg-emerald-400 pulse-dot" : "bg-slate-500"}`} aria-hidden />
              <span className="font-medium text-slate-100">{version?.executando ? "Agente operando" : "Agente em pausa"}</span>
            </div>
            <div className="flex items-center gap-2 text-xs text-slate-400" aria-label="Relógio simulado">
              <Cpu size={14} aria-hidden /> <span className="hidden sm:inline">Relógio simulado</span> <span className="num whitespace-nowrap font-mono text-slate-200">{ts(version?.sim_now)}</span>
              <div className="hidden h-1.5 w-28 overflow-hidden sm:block rounded bg-white/5" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100} aria-label="Progresso da simulação">
                <div className="h-full bg-brand-500 transition-all" style={{ width: `${progress}%` }} />
              </div>
              <span className="num whitespace-nowrap">{version?.tick ?? 0}/168 h</span>
            </div>
            {version?.erro && <Badge tone="danger">Erro: {version.erro}</Badge>}
            <div className="ml-auto flex items-center gap-2">
              <span className="text-xs text-slate-400 md:hidden">{inst.nome}</span>
              {sess.user.demo && <Badge tone="demo" title="Login simulado — modo demonstração">DEMO</Badge>}
              <div className="text-right text-xs leading-tight">
                <div className="font-medium text-slate-100">{sess.user.nome}</div>
                <div className="text-slate-400">{sess.user.papel}{sess.user.pessoa_id ? ` · ${sess.user.pessoa_id}` : ""}</div>
              </div>
              <Button tone="ghost" onClick={logout} title="Sair"><LogOut size={16} aria-label="Sair" /></Button>
            </div>
          </header>
          <nav aria-label="Módulos (móvel)" className="flex gap-1 overflow-x-auto border-b border-white/5 px-2 py-1 md:hidden">
            {NAV.map((n) => (
              <button key={n.id} onClick={() => nav(iid, n.id)} className={`whitespace-nowrap rounded px-2 py-1 text-xs ${page === n.id ? "bg-brand-500/15 text-brand-300" : "text-slate-400"}`}>{n.label}</button>
            ))}
          </nav>
          <main id="main" className="mx-auto w-full max-w-[1400px] flex-1 p-4 md:p-6">
            {page === "overview" && <Overview />}
            {page === "journey" && <Journey />}
            {page === "cases" && <Investigations />}
            {page === "approvals" && <Approvals />}
            {page === "agent" && <AgentWorkspace />}
            {page === "sim" && <Simulation instances={instances} />}
            {page === "notifications" && <Notifications />}
          </main>
        </div>
      </div>
    </AppCtx.Provider>
  );
}

function OperatorGate({ onOk, demo }: { onOk: () => void; demo: boolean }) {
  const [v, setV] = useState("");
  return (
    <div className="mx-auto max-w-lg p-10">
      <Card title="Acesso de operador" icon={<KeyRound size={16} aria-hidden />}>
        <p className="text-sm text-slate-300">A administração de instâncias exige o token de operador do servidor{demo ? " (no modo demonstração ele só é entregue a clientes no próprio host)" : " (GP_ADMIN_TOKEN)"}. O token fica só na memória desta aba.</p>
        <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); setOperator(v.trim()); onOk(); }}>
          <label htmlFor="op" className="sr-only">Token de operador</label>
          <input id="op" type="password" value={v} onChange={(e) => setV(e.target.value)} className="flex-1 rounded-md border border-white/10 bg-ink-850 px-2 py-1.5 text-sm text-slate-100" />
          <Button type="submit" tone="primary">Entrar</Button>
        </form>
      </Card>
    </div>
  );
}

function DemoLogin({ iid, demo, onLogged }: { iid: string; demo: boolean; onLogged: () => void }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => {
    if (demo) api<User[]>(`/api/instances/${iid}/auth/demo-users`).then(setUsers).catch(setErr);
  }, [iid, demo]);
  const login = (u: string) =>
    api<{ token: string; usuario: User }>(`/api/instances/${iid}/auth/demo-login`, { method: "POST", body: { login: u } })
      .then((r) => { sessions.set(iid, r.token, r.usuario); onLogged(); })
      .catch(setErr);
  if (!demo)
    return <div className="mx-auto max-w-lg p-10"><Card title="Autenticação"><p className="text-sm text-slate-300">Login via provedor de identidade (SSO) não está configurado nesta entrega. O login por identificador existe apenas em modo demonstração (<code>GP_DEMO_MODE=1</code>).</p></Card></div>;
  const groups = (users || []).reduce<Record<string, User[]>>((a, u) => ((a[u.papel] = [...(a[u.papel] || []), u]), a), {});
  return (
    <div className="mx-auto max-w-3xl p-6 md:p-10">
      <div className="mb-6 rounded-xl border border-fuchsia-400/30 bg-fuchsia-500/10 p-4 text-sm text-fuchsia-100">
        <strong>Modo demonstração.</strong> O login abaixo é simulado: escolha uma identidade de teste do cadastro (usuarios_sistema). Papéis e alçadas são os do ERP e
        continuam sendo verificados pelo motor de políticas no servidor.
      </div>
      <ErrorBox error={err} />
      {!users ? <Spinner /> : (
        <div className="grid gap-4 md:grid-cols-3">
          {Object.entries(groups).map(([papel, us]) => (
            <Card key={papel} title={papel}>
              <ul className="space-y-1.5">
                {us.map((u) => (
                  <li key={u.login}>
                    <button onClick={() => login(u.login)} className="w-full rounded-lg px-2 py-1.5 text-left text-sm hover:bg-white/5">
                      <div className="font-medium text-slate-100">{u.nome}</div>
                      <div className="text-[11px] text-slate-400">{u.login}{u.pessoa_id ? ` · ${u.pessoa_id}` : ""} · alçada {u.alcada_cents === null ? "sem limite" : (u.alcada_cents / 100).toLocaleString("pt-BR")}</div>
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function InstancePicker({ instances, demo, onPick, onCreated }: { instances: Instance[]; demo: boolean; onPick: (id: string) => void; onCreated: () => void }) {
  const [name, setName] = useState("Demonstração");
  const [sim, setSim] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const create = () => {
    setBusy(true);
    api<{ id: string }>("/api/instances", { method: "POST", body: { name, approval_simulator: demo && sim } })
      .then((r) => { onCreated(); onPick(r.id); })
      .catch(setErr)
      .finally(() => setBusy(false));
  };
  return (
    <div className="mx-auto max-w-3xl p-6 md:p-12">
      <div className="mb-8 flex items-center gap-3">
        <div className="grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-brand-400 to-indigo-500 text-ink-950"><ShieldCheck size={24} aria-hidden /></div>
        <div>
          <h1 className="text-2xl font-semibold text-white">GuardianPay AI</h1>
          <p className="text-sm text-slate-400">Centro de comando de operações financeiras autônomas · contas a pagar</p>
        </div>
      </div>
      <Card title="Instâncias de simulação isoladas" icon={<FlaskConical size={16} aria-hidden />}>
        {instances.length === 0 ? <p className="mb-4 text-sm text-slate-400">Nenhuma instância. Crie uma para começar (ingestão dos 24 CSVs em ~1 s).</p> : (
          <ul className="mb-5 divide-y divide-white/5">
            {instances.map((i) => (
              <li key={i.id} className="flex items-center justify-between py-2.5">
                <div>
                  <div className="text-sm font-medium text-slate-100">{i.nome} <span className="font-mono text-[11px] text-slate-500">{i.id}</span></div>
                  <div className="text-xs text-slate-400">{ts(i.sim_now)} · tique {i.tick}/168 {i.simulador_aprovacao && <Badge tone="demo">simulador de aprovação</Badge>}</div>
                </div>
                <Button onClick={() => onPick(i.id)}>Abrir</Button>
              </li>
            ))}
          </ul>
        )}
        <div className="flex flex-wrap items-end gap-3 border-t border-white/5 pt-4">
          <label className="text-xs text-slate-400">Nome<input value={name} onChange={(e) => setName(e.target.value)} className="mt-1 block rounded-md border border-white/10 bg-ink-850 px-2 py-1.5 text-sm text-slate-100" /></label>
          {demo && (
            <label className="flex items-center gap-2 text-xs text-slate-300">
              <input type="checkbox" checked={sim} onChange={(e) => setSim(e.target.checked)} /> Simulador de aprovações de TESTE
            </label>
          )}
          <Button tone="primary" onClick={create} busy={busy}><Plus size={14} aria-hidden /> Nova instância</Button>
        </div>
        <ErrorBox error={err} />
      </Card>
    </div>
  );
}
