import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useApp } from "./App";

/** Busca um recurso da instância e refaz quando a versão do servidor muda (polling leve em /version). */
export function useLive<T>(path: string | null, extra: unknown[] = []) {
  const { iid, version } = useApp();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const seq = useRef(0);
  const sig = version ? `${version.tick}|${version.audit}|${version.transicoes}|${version.chat}|${version.executando}` : "";
  const load = useCallback(() => {
    if (!path) return;
    const my = ++seq.current;
    api<T>(path, { iid })
      .then((d) => { if (my === seq.current) { setData(d); setError(null); } })
      .catch((e) => my === seq.current && setError(e));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, iid, ...extra]);
  // troca de instância/recurso: descarta o dado anterior para nunca exibir o histórico de outra instância
  useEffect(() => {
    seq.current++;
    setData(null);
    setError(null);
  }, [path, iid]);
  useEffect(() => {
    load();
  }, [load, sig]);
  return { data, error, reload: load };
}
