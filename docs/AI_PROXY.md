# AI Proxy — referência usada pelo GuardianPay

Fonte: guia oficial do AI Proxy distribuído pela organização do evento (skill `ai-proxy` da GoGroup). O repositório
não contém outra documentação do proxy; nada abaixo foi inferido.

| Item | Valor documentado |
|---|---|
| Base URL | `https://ai-proxy.gogroupbr.com/v1` (formato OpenAI) |
| Autenticação | `Authorization: Bearer <token gw-tok-…>` + `Content-Type: application/json`; token só via `AI_PROXY_API_KEY` |
| Geração | `POST /v1/chat/completions` → `choices[0].message.content`; uso em `usage.*`; modelo servido no header `x-model-served` |
| Listagem | `GET /v1/models` (opcional para o GuardianPay — não é pré-requisito da geração) |
| Assíncrono | `POST /v1/chat/jobs` + `GET /v1/chat/jobs/{id}?wait=25` (para modelos lentos) |
| Decisão tipada | `POST /v1/systemone` (Jev) |
| Limites | ~250 req/min por token (`x-ratelimit-*`); rotas síncronas cortadas em ~125 s (erro 524 do Cloudflare) |

**Modelos documentados:** `latest` (aponta para o mais avançado; o guia recomenda usar em apps), `gpt-6-astra`,
`gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol` (raciocínio de 60–95 s: usar chat/jobs, não recomendado aqui), `gpt-5.6-terra`,
`gpt-5.6-luna`, `glm-5.3` (só texto), `glm-5.3-flash`, `mimo-v2.6-pro`, `mimo-v2.6-flash`, `k3.1-pro`, `kimi-for-coding`.
Modelos "extras" (glm/mimo/kimi) exigem `max_tokens ≥ 2000`. `deepseek-*` é **proibido no evento** (bloqueado no cliente).
O guia não nomeia um modelo exclusivo do evento; o GuardianPay usa `AI_PROXY_MODEL` (padrão sugerido: `latest`).

## Configuração e teste local (Windows/PowerShell)
```powershell
# .env na raiz do projeto (fora do git): AI_PROXY_API_KEY=<seu token>  e  AI_PROXY_MODEL=latest
$env:PYTHONPATH="src"; python -m guardianpay llm-check     # 1 geração mínima; não imprime a credencial
```
Interpretação do resultado (`resultado.tipo`):
- `LLMAuthError` (401): token ausente/inválido. Use o valor puro (sem `Bearer`, sem aspas).
- `LLMForbidden` / `LLMModelListForbidden` (403): recusa de autorização. Se a resposta citar Cloudflare/`error code: 1010`,
  é bloqueio do WAF à assinatura do cliente HTTP (o cliente agora envia `User-Agent` explícito); se for só `/v1/models`,
  basta manter `AI_PROXY_MODEL` definido — a geração não depende da listagem.
- `LLMModelInvalid` (400/404/422 citando o modelo): identificador inexistente ou não liberado para o token.
- `LLMUnavailable`: rede, 429 após espera, 5xx ou timeout. O agente segue no modo determinístico.

## Runtime do LLM no GuardianPay (correção pós-Workspace)
- **Um cliente por instância** (`build_client` em `runtime.py`): disjuntor (`llm/breaker.py`) e orçamento (`llm/budget.py`)
  persistidos no banco da instância (`llm_breaker`, `llm_breaker_log`, `llm_usage`). Reiniciar o servidor não zera
  consumo nem esconde um disjuntor aberto; instâncias não compartilham estado. A concorrência (padrão 2) é por processo,
  porque o rate limit do proxy é do token.
- **Disjuntor:** CLOSED → OPEN (2 falhas transitórias seguidas, ou 1 permanente: 401/403/modelo recusado) → HALF_OPEN
  (após o cooldown, UMA chamada de teste) → CLOSED. Saída do modelo inválida/truncada NÃO conta (o proxy respondeu);
  HTTP 200 com HTML (página do gateway) conta. Recuperação controlada: `POST /api/instances/{iid}/llm/disjuntor/recuperar`
  (token de operador) leva OPEN → HALF_OPEN, sem zerar uso.
- **Timeout:** padrão 90 s (máx. 120). Timeout de leitura **não é reenviado** (a requisição já foi processada/cobrada);
  só falhas de conexão, 429 (Retry-After) e 5xx/524 são reenviadas.
- **Orçamento:** instância (60 chamadas / 150 mil tokens), cota do agente autônomo (60%), tarefa (3 chamadas, 24 mil
  tokens, 110 s), ciclo autônomo (3 casos com LLM e 120 s por tique). Mensagens dizem a causa exata.
- **Diagnóstico:** `GET /api/instances/{iid}/llm/diagnostico` (sessão) — estado do cliente, modelo, última chamada
  (tentou?, etapa, tipo de falha, HTTP, tentativas, latência, tokens), disjuntor (estado, causa, recupera em),
  orçamento restante, últimas chamadas e transições. Nunca inclui token ou cabeçalhos.
- **Validação da investigação real:** `python -m guardianpay llm-check --tarefa` roda a mesma tarefa composta do
  Workspace (até 3 chamadas) num banco temporário, sem ciclo autônomo, e imprime o diagnóstico e a linha 💲 de cada chamada.
