// Cliente da API. O token de sessão é emitido pelo servidor; o frontend nunca envia actor_id.

export type User = { login: string; nome: string; papel: string; pessoa_id: string; alcada_cents: number | null; demo: boolean };
export type Instance = { id: string; nome: string; sim_now: string; tick: number; executando: boolean; simulador_aprovacao: boolean };
export type Version = { tick: number; sim_now: string; executando: boolean; audit: number; transicoes: number; chat: number; erro: string | null };

const KEY = "gp.sessions";

function loadTokens(): Record<string, { token: string; user: User }> {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || "{}");
  } catch {
    return {};
  }
}

export const sessions = {
  get(iid: string) {
    return loadTokens()[iid];
  },
  set(iid: string, token: string, user: User) {
    const all = loadTokens();
    all[iid] = { token, user };
    try {
      sessionStorage.setItem(KEY, JSON.stringify(all));
    } catch {
      /* armazenamento indisponível: sessão só em memória nesta aba */
    }
  },
  clear(iid: string) {
    const all = loadTokens();
    delete all[iid];
    try {
      sessionStorage.setItem(KEY, JSON.stringify(all));
    } catch {
      /* ignore */
    }
  },
};

// Token de operador (administração de instâncias/integrações). No modo demo é obtido do servidor LOCAL; nunca persistido.
let operatorToken: string | null = null;
export const setOperator = (t: string | null) => { operatorToken = t; };
export const hasOperator = () => !!operatorToken;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function newKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; iid?: string; idem?: string; headers?: Record<string, string> } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (opts.iid) {
    const s = sessions.get(opts.iid);
    if (s) headers.Authorization = `Bearer ${s.token}`;
  }
  if (opts.idem) headers["Idempotency-Key"] = opts.idem;
  if (operatorToken) headers["X-GP-Operator"] = operatorToken;
  const res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) throw new ApiError(res.status, (data && (data.detail || data.error)) || res.statusText);
  return data as T;
}

export const brl = (cents: number | null | undefined) =>
  cents === null || cents === undefined ? "—" : (cents / 100).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

export const ts = (iso?: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};
