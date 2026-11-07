# PROJECT_STATE — GuardianPay AI

_Iteração 3.2 (runtime LLM: disjuntor, orçamento, diagnóstico; sobre a 3.1) — relógio de referência 02/11/2026 08:00 → 09/11/2026 08:00._

## Arquitetura vigente
- **Stack:** Python 3.11+ · SQLite (WAL, transações/savepoints) · Pydantic v2 · pytest. Sem framework de agentes.
  LLM opcional via AI Proxy (`llm/proxy_client.py`); FastAPI/React na entrega de interface.
- **Camadas de dados:** `raw_*` (CSV fiel, somente leitura) · `n_*` (derivado) · `op_*` (estado mutável, só via executor).
- **Ação:** `ToolRequest` → `ToolExecutor` → `PolicyEngine` (catálogo, ator do registro/sessão, parâmetros, evidências,
  corte do lote pelo relógio, alçada/antifracionamento, aprovação dupla, aprovação válida) → efeito em SAVEPOINT →
  verificação → `audit_log` (hash encadeado) + `events` + `tool_calls` (idempotência).
- **Agente** (`agent/orchestrator.py`): OBSERVAR (eventos em ordem `sim_ts, seq`; dead-letter após 3 falhas) → TRIAR
  (`auditing/` gera Findings → casos idempotentes) → INVESTIGAR (re-validação no estado atual) → DECIDIR
  (`agent/decision.py`, tabela determinística) → pré-checagem de política → AGIR (chaves `case_id:ação`) →
  VERIFICAR (lê `op_*` de volta) → REGISTRAR (`case_decisions` com justificativa e referências de política).
  Casos contidos ficam `awaiting_approval`/`escalated` — nunca "resolvidos" automaticamente.
- **Tempo** (`sim/scheduler.py`): a cada tique, primeiro fecha lotes com corte vencido (dia útil; 10h MER / 14h LIT;
  tesoureiro assina sem conferir) e depois o agente age. Lote fechado/corte vencido ⇒ retirada/bloqueio negados.
- **Placar** (`sim/scoreboard.py`): perda simulada, evitado realizado/projetado, custo de atraso (7 dias, multa+juros+relacionamento),
  acionamentos. Sem gabarito: "indevido" = exposição de casos comprovados pelo motor.
- **Autenticação** (`auth.py`): clientes externos só via sessão (token opaco, hash no banco, expira, revogável); `actor_id`
  enviado pelo cliente é ignorado; agentes não recebem sessão externa. CLI usa login simulado + sessão.
- **LLM:** só redige nota contextual de casos de fraude (dados de terceiros envelopados); nunca decide valor, permissão ou ação.
  Modelo escolhido entre `/v1/models` (sem presumir `latest`; `deepseek-*` bloqueado); timeout, retry limitado, 429 com
  `retry-after`, resposta malformada tipada, disjuntor; uso gravado em `llm_usage`.

## Entrega 4 — versão final de submissão
- Congelada: sem novas arquiteturas. Ajustes pontuais: ferramentas negadas listadas como negadas (`ferramentas_negadas`,
  badge "negada", texto "Ferramentas NEGADAS (não executadas)"); avaliação da IA pedida em até ~900 caracteres (cabe no
  limite de tokens, que foi mantido) e `llm-check --tarefa` imprime a avaliação inteira; painel rotula "evitado" como estimativa.
- Validação real informada pelo time (Windows): `llm-check --tarefa` com gpt-6-astra — 2 chamadas, 5.314 tokens,
  disjuntor CLOSED, sem mutação. Validação local (sandbox, sem acesso ao proxy): 158 testes, build, jornada E2E.
- Materiais em `docs/submissao/`.

## Correção crítica do runtime LLM (3.2)
- **Causa-raiz (no código; reproduzida com transporte simulado, não com o proxy real):** o cliente do runtime era criado
  com `timeout=20`, `max_retries=1` e `breaker_threshold=1` (cooldown 300 s), diferente do `llm-check`, que envia
  ~20 tokens com `max_tokens=16`. A tarefa composta envia milhares de tokens de evidência com `max_tokens=1800` a um
  modelo de raciocínio: a resposta passa de 20 s → `TimeoutError` (tratado como erro de rede e **reenviado**, dobrando
  custo/espera) → `LLMUnavailable` → disjuntor aberto na 1ª falha por 5 min. O mesmo cliente é usado pelo agente
  autônomo, que no tique 0 tenta o LLM em 7 casos (o 1º é o próprio R5-TP431754): após reiniciar, o ciclo abriu o
  disjuntor antes da conversa → "não usado: disjuntor aberto ou orçamento esgotado". A falha ocorre DURANTE a chamada
  (leitura da resposta), não antes do HTTP nem no parsing.
- **Correções:** `llm/breaker.py` (CLOSED/OPEN/HALF_OPEN explícito, falha transitória × permanente, causa registrada,
  cooldown configurável, uma tentativa em HALF_OPEN, persistido por instância, recuperação controlada pelo operador);
  `llm/budget.py` (limites configuráveis por instância, canal autônomo × interativo, tarefa e ciclo; causa específica);
  `llm/jsonout.py` (JSON em cercas/partes em lista; truncado/vazio = rejeitado); `proxy_client.py` (timeout 90 s
  configurável ≤120, timeout não reenviado, `finish_reason=length` detectado, saída rejeitada não abre disjuntor,
  HTML com 200 conta como falha do gateway, concorrência por processo, `diagnostics()` sem credencial, uso com canal,
  HTTP, latência e tipo de erro em `llm_usage`). Reasoner/Workspace usam a causa exata e o orçamento da tarefa.
- **Observabilidade:** `diagnostico_ia` em cada tarefa composta (usada / não usada pelo roteador / orçamento /
  disjuntor / chamada falhou / saída rejeitada + modelo, tentativa, HTTP, tentativas, tokens, orçamento restante,
  disjuntor, recupera em, motivo); `GET /api/instances/{iid}/llm/diagnostico`; `POST …/llm/disjuntor/recuperar`;
  `llm-check --tarefa` (mesma chamada do Workspace num banco temporário).
- **Isolamento:** instância nova nasce com `audit_log`, `cases` e `chat_messages` vazios (cópia do template só com
  ingestão). As "4 ações anteriores" eram do próprio agente autônomo daquela instância no tique 0 (`bloquear_titulo`,
  `abrir_incidente`, `acionar_pessoa`×2). A interface agora lista essas ações com horário, ator e "nesta instância";
  `useLive` descarta dados ao trocar de instância. Nada foi apagado.
- **Testes:** `tests/test_llm_runtime.py` (25, transporte HTTP simulado). Suíte: **157 passed**. E2E Playwright ok.
- **Pendente:** validação real do `gpt-6-astra` (o sandbox recebe 403 no CONNECT ao proxy): rodar
  `python -m guardianpay llm-check --tarefa` no Windows.

## Correção do Agent Workspace (pós AI Proxy)
- **Diagnóstico:** em `api/agent_chat.py::interpret`, a regex `DONE` ("executou|ações executadas|…") era avaliada antes das
  intenções com entidade e casava "execute as ações autorizadas"/"inconsistências encontradas" → `acoes_executadas` (consulta
  global da auditoria). Não havia intenção multi-objetivo: tudo era reduzido a uma intenção. O LLM só era tentado em
  `ajuda/explicar…`; quando `rt.llm` era `None` (modo off/sem chave/runtime criado antes do `.env`) o rótulo saía
  "deterministica" sem explicar por quê.
- **Correções:** `DONE` só vale sem entidade (TP/NF/caso/fornecedor/lote). Novo `api/composite.py` (`CompositeTask`):
  pedidos com entidade + ≥2 objetivos (investigar/cruzar/avaliar/explicar/verificar/executar) seguem o pipeline
  leituras reais (consultar_titulo, consultar_fornecedor, consultar_nf, buscar_titulos) → motor de auditoria (DDA, extrato,
  cadastro, comunicações → evidências E#) → raciocínio com o modelo configurado (opcional; leituras extras só do catálogo de
  leitura; inconsistências só com R#/E# existentes) → propostas da IA validadas pela guarda do `ContextualReasoner` e pelo
  `PolicyEngine` → execução só se o pedido for imperativo, sempre pelo `ToolExecutor` → releitura do título e da auditoria.
  O orquestrador atua só nos casos daquele título (`Orchestrator.work_on`), inclusive após aprovação concedida.
  Pedido só de contenção ("reter TP…") continua no caminho determinístico `conter_titulo`.
- **Observabilidade:** payload/UI separam objetivos, modelo de IA (`usado: <modelo> (validado)` / `não usado: <motivo>` /
  `falhou → fallback determinístico (<erro>)`), ferramentas planejadas × executadas, evidências, inconsistências, dados
  ausentes, ações propostas × realizadas agora × anteriores, aprovações pendentes e resultado verificado (antes/depois).
  Consultas simples dizem "LLM não usado (<motivo>)"; "fallback" só aparece em falha real.
- **Testes:** `tests/test_workspace_fix.py` (13, LLM por MOCK identificado). Suíte: **132 passed**.

## Correção AI Proxy (pós 3.1)
- Causa no código: `resolve_model()` chamava `GET /v1/models` mesmo com `AI_PROXY_MODEL` definido — um 403 na listagem
  derrubava toda a geração. Agora o modelo explícito é usado direto; a listagem é opcional.
- Cliente envia `User-Agent` explícito (o padrão `Python-urllib` costuma ser barrado por WAF/Cloudflare, que fica à frente do
  proxy segundo o guia); 4xx classificados (`LLMAuthError`, `LLMForbidden`, `LLMModelListForbidden`, `LLMModelInvalid`) com
  diagnóstico sem credencial; `.env` lido com `utf-8-sig`; chave normalizada (aspas, BOM, prefixo `Bearer`).
- `python -m guardianpay llm-check` e `POST /api/integrations/llm/check`: 1 geração mínima com o modelo configurado.
- Referência: `docs/AI_PROXY.md` (guia oficial do evento). Testes: 119 passed.

## Entrega 3.1
- **AI Proxy:** chave presente no `.env` (valor não exibido). Validação 1/3: `GET /v1/models` → o gateway de saída deste
  sandbox responde 403 ao CONNECT para `ai-proxy.gogroupbr.com:443` (antes do TLS; a credencial não trafega). 0 chamadas de
  chat, 0 tokens. Fallback determinístico ativo; UI e chat mostram "LLM indisponível → determinístico". Disjuntor abre na
  1ª falha no runtime (sem travar ciclos).
- **Segurança da API:** token de operador (`X-GP-Operator`) para listar/criar/excluir instâncias e para status/verificação do
  LLM; no demo o token é gerado por processo e entregue só a loopback; login demo, usuários demo e controles de simulação só
  com demo + loopback; `main()` recusa demo fora de 127.0.0.1; `/health` não expõe inventário; `DELETE /api/instances/{id}`.
- **Agent Workspace:** linguagem natural determinística (fornecedor por nome, "NF n", "por que…", "quais ações…"),
  contradições a partir das evidências, dados ausentes, leituras planejadas por regra (executadas e referenciadas),
  esclarecimento com retomada (`chat_pending`), relato do que foi executado (auditoria). `ChatReasoner`: LLM (quando houver)
  só lê via executor, referências R# conferidas (inventadas descartadas; sem referência válida → determinístico),
  escalonamento só abre caso se as regras confirmarem.
- **Demonstração:** `/demo/journey` + página "Jornada guiada" (cenários A e B, cada etapa verificada no banco);
  painel "Agora" (estado, próximo corte, ferramentas); rótulos "Evitado (lotes já enviados)" × "Evitável (futuros)".
- **Testes:** instâncias copiadas de um template ingerido (API 15,8 s → 9,6 s). Slow tests restantes (~1,5 s) executam
  ciclos reais completos; nenhuma cobertura removida.

## Entrega 3 — Command Center
- **API** (`api/app.py`, FastAPI): instâncias isoladas (`api/instances.py`: um banco, Runtime, lock e sessões por instância;
  thread no servidor executa o agente mesmo com a tela fechada); sessões por `Authorization: Bearer` (validade em relógio real);
  login simulado só com `GP_DEMO_MODE=1`; `/auth/sso` reservado (501). Comandos de simulação e decisões exigem/aceitam
  `Idempotency-Key` (`api_commands`). Aprovar/recusar usa `execute_with_session` e dispara `orchestrator.cycle()` (retomada).
  Atualização da UI por polling leve de `/version` (sem reexecutar simulação).
- **Agent Workspace** (`api/agent_chat.py`): intenção determinística (LLM só se disponível, rotulado), leituras via
  ToolExecutor com referências `R#`, abre caso quando a regra comprova e deixa o Orchestrator agir; recusa retenção sem evidência.
- **Interface** (`web/`, React 18 + TS + Tailwind, Lucide): Centro de Operações, Investigações, Aprovações, Agente,
  Simulação (passo/avançar/contínuo/pausa/reset/simulador de teste/eventos/comparação/estado real do AI Proxy), Notificações (outbox).
  Fatos x estimativas separados; confirmado x evidência forte x suspeita diferenciados; DEMO e aprovações simuladas rotulados.
- Aprovações agora guardam `case_id`; sessões expiram por relógio real (avançar a simulação não derruba usuários).

## Entrega 2.1
- **Desempenho:** 168 h em 2,0 s (antes 44,5 s; 22×); consultas 27.143 → 9.584. Causa: `AuditContext` reconstruído a cada
  tique (41 s) e polling de aprovações (2×7.728 consultas). Correção: contexto preguiçoso por ciclo, status de aprovação em
  lote, cache da decodificação de boleto. Indicadores idênticos ao baseline (teste `test_full_simulation_matches_pre_optimization_baseline`).
  Os 8 lotes fechados são todos os que fecham no período; os 9 restantes fecham após 09/11 08h (avaliados no estado final).
- **IA no ciclo** (`agent/reasoner.py`): para R3/R4/R5/R5F e indícios com título, o LLM investiga (só ferramentas de leitura,
  via executor), aponta faltas/ambiguidade e propõe ações. Guarda: catálogo, escopo do caso, referências E#/T# existentes,
  identidade escolhida pelo sistema, portão de risco (contenção só com evidência comprovada/forte), dry-run do PolicyEngine;
  plano determinístico é piso não removível. Limites: 3 passos, 3 chamadas e 5 leituras por caso, 3 ações por plano,
  1.500 tokens/chamada, orçamento global (60 chamadas/150 mil tokens); disjuntor com meio-aberto após resfriamento.
  Trilhas em `ai_traces`.
- **Aprovação ponta a ponta:** roteamento para a pessoa correta (`approvals.py`, coluna `routed_to`, aviso no outbox),
  revalidação antes de executar (versão dos títulos, elegibilidade, aprovador ativo, PolicyEngine com a aprovação),
  execução, verificação e reagendamento. Simulador **de teste** (`sim/approver.py` + cenário JSON) com identidades explícitas,
  só em expediente, comentário `[SIMULAÇÃO DE TESTE]` e tabela `simulated_decisions`.
- **Identidade:** login por identificador só com `GP_DEMO_MODE=1` (sessões `demo:*` rejeitadas fora desse modo);
  caminho da API: `issue_from_identity_provider` (token do IdP verificado → e-mail corporativo → usuário do ERP).
- **Indicadores** (`sim/scoreboard.py`): atribuição única por título; pagamentos processados, retenções executadas, exposição,
  economia realizada/projetada, saneado antes do pagamento, perda, atraso, indícios não verificáveis.

## Regras de auditoria (`auditing/rules.py`)
R1 duplicidade (raiz CNPJ + NF normalizada + parcela; séries distintas não são duplicidade) · R1H duplicidades históricas pagas ·
R2 título×duplicata (tolerância R$ 0,10; multa 2% + juros pro rata) e NF×OC (2% ou R$ 50) · R3 já pago (DDA liquidado +
débito avulso no extrato não explicado) · R4 troca bancária sem ligação ao telefone anterior (+ domínio semelhante/injeção) ·
R5 beneficiário do boleto com outra raiz de CNPJ / fornecedor errado (NF e OC de outra empresa) · R6 sem NF/OC aprovada
(dispensas utilidades/aluguel), fracionamento, aprovações históricas · R7 comunicações suspeitas (phishing, domínio semelhante,
remetente não oficial, instruções a automação). Um título recebe uma única ação principal (subsunção por prioridade).

## Concluído (com teste)
- Entrega 1 (ingestão, normalização, relógio, políticas, executor, auditoria) — mantida.
- Motor de auditoria, orquestrador, decisões, contenção verificada, acompanhamento de aprovações, retomada, fechamento de lotes,
  placar, sessões, cliente LLM endurecido, novos adaptadores (`corrigir_lancamento`, `cancelar_titulo`,
  `reverter_alteracao_cadastro`, `abrir_incidente`).

## Testes
`python -m pytest -q` → **158 passed** (inclui 14 da correção do Agent Workspace e 25 do runtime LLM). `npm run build` ok.

## Resultado da simulação completa (168 tiques, LLM desligado)
58 casos; 150 ações executadas no tique 0; 8 lotes fechados; perda simulada R$ 0; evitado realizado R$ 359.336,95;
evitado projetado (lotes após 09/11 08h) R$ 288.873,80; custo de atraso estimado R$ 31.845,83 (principalmente retenção dos
títulos legítimos da Embalarte até a reversão do cadastro); 9 acionamentos, todos ligados a casos comprovados.

## Resultado com o simulador de TESTE de aprovações (GP_DEMO_MODE=1)
54 casos resolvidos, 4 escalados (recuperação histórica, Embalarte aguardando desbloqueio após reversão do cadastro com aprovação
dupla, boleto Pigmentos aguardando fornecedor, OC cancelada recusada); 50 aprovações e 1 recusa simuladas; perda R$ 0;
saneado antes do pagamento R$ 135.198,92; atraso estimado R$ 26.522,03.

## Pendências
- Sem desbloqueio no catálogo: títulos da Embalarte seguem bloqueados após a reversão do cadastro.
- AI Proxy não alcançável deste sandbox (egress 403 em `ai-proxy.gogroupbr.com`): cliente e reasoner validados só com mocks;
  qualidade real das análises do modelo ainda não medida.
- Adaptadores ainda `not_implemented`: bloquear_fornecedor, propor_correcao_processo, aprovar_alteracao_bancaria, liberar_lote,
  responder_colaborador. Feed horário de novos títulos/mensagens continua inexistente no ZIP.
- Desbloqueio de título pelo aprovador não tem ferramenta no catálogo.

## Limitações da Entrega 3
- Provedor de identidade não configurado (só modo demonstração); canais externos (Slack/Telegram) não integrados — outbox é simulado.
- AI Proxy segue inacessível deste sandbox (403 no gateway de saída): raciocínio LLM validado só com mocks identificados.
- Polling em vez de SSE; uma thread de execução para todas as instâncias; SQLite local (um processo).

## Próxima tarefa
Entrega 3 — API FastAPI sobre `Runtime` (sessão via IdP; demo só com flag) + central de operações. Antes: validar o reasoner
com o AI Proxy real (5–10 chamadas) num ambiente com egress liberado.

## Decisões importantes
- Perfis ERP → papéis: diretoria e tesouraria = `aprovador` (alçada preservada). Identidade por `pessoa_id`.
- Cancelamento, troca de fornecedor e reversão de cadastro sempre via aprovação (irreversíveis), mesmo dentro da alçada.
- Correção de valor ≤ R$ 5 mil para o valor da duplicata é autônoma (política permite; evita atraso).
- Bloqueio (auditor) para fraude; retirada do lote (analista) para duplicidade/já pago/valor/fornecedor/aprovação.
- `.env` é o único lugar da credencial; `.env.example` fica sem valor.
