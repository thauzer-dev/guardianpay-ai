# Arquitetura multiagente e ações autônomas

```
CSVs (24) → ingestão raw_* (imutável) → n_* (normalizado) → op_* (estado operacional)
                                    │
 Relógio simulado (tiques de 1 h, cortes de lote 10h/14h) ── eventos ──► ORQUESTRADOR (agent:orchestrator)
                                    │                                     │ triagem → casos (máquina de estados)
          Motor de auditoria R1–R7 (agent:financial_audit) ◄──────────────┤ investigação / revalidação
          Raciocínio contextual GPT-6 Astra (ContextualReasoner) ◄────────┤ proposta validada (guarda + dry-run)
          Risco e fraude (agent:risk_fraud) · Tesouraria (agent:treasury) │
          Colaboração/aprovações (agent:approval_collab) ◄────────────────┤ roteamento à pessoa certa, retomada
                                    ▼
          ToolExecutor → PolicyEngine (catálogo, papel, alçada, corte do lote, aprovação dupla, idempotência)
                     → efeito em SAVEPOINT → verificação (relê op_*) → audit_log com hash encadeado + eventos
```

**Ciclo autônomo (servidor, independe de tela aberta):** observar → triar → investigar → decidir → pré-checar
política → agir → verificar → registrar. Casos: detected → investigating → authorized → executing →
awaiting_approval / escalated / resolved.

**Ações autônomas (ferramentas do catálogo):** `bloquear_titulo`, `retirar_do_lote`, `corrigir_lancamento`
(autônoma até R$ 5 mil para o valor da duplicata; acima, aprovação), `cancelar_titulo` e `reverter_alteracao_cadastro`
(sempre com aprovação), `abrir_incidente`, `acionar_pessoa`, `contatar_fornecedor`, além das leituras.

**IA (AI Proxy, gpt-6-astra):** pode analisar evidências de vários sistemas, pedir ferramentas de LEITURA, apontar dados
faltantes, propor ações citando evidências E#/R#. Não pode conceder permissão, escolher identidade, aprovar, inventar
evidência, remover a contenção determinística nem agir fora do título/caso. Toda proposta passa pela guarda e pelo
PolicyEngine; texto de terceiros é marcado como dado não confiável.

**Agent Workspace:** roteamento híbrido — consultas simples determinísticas; pedidos compostos (investigar + cruzar +
explicar/executar) seguem investigar → cruzar → avaliar (IA) → propor → políticas → executar (só se pedido) → verificar,
separando planejado × executado, proposto × realizado, negado × executado.

**Confiabilidade do LLM:** cliente por instância; disjuntor CLOSED/OPEN/HALF_OPEN persistido; orçamento por instância,
canal (autônomo × conversa), tarefa e ciclo; timeout 90 s sem reenvio; diagnóstico sem credencial; fallback
determinístico explícito.

**Aprovação e retomada:** solicitações roteadas por papel/alçada a pessoas reais do cadastro; sessão por usuário (a IA
nunca aprova); após aprovação o orquestrador revalida o estado (versão do título) e só então executa.
