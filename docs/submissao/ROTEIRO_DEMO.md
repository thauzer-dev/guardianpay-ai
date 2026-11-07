# Roteiro de demonstração (3 min) — passos e resultados esperados

Preparação: servidor com `GP_DEMO_MODE=1` e `.env` com `AI_PROXY_MODEL=gpt-6-astra`, `GP_LLM_MODE=auto`
(antes, `python -m guardianpay llm-check --tarefa` deve terminar com IA "usada"). Abrir http://127.0.0.1:8000.
Os passos 1–3, 5 e 6 são cobertos pela jornada E2E automatizada (`web/e2e/demo-journey.cjs`, LLM desligado).

| Tempo | Passo | Resultado esperado |
|---|---|---|
| 0:00 | Criar instância com o simulador de aprovação **desligado**; entrar como **Larissa Gomes (analista)** | Instância nova, sem ações registradas |
| 0:15 | Jornada guiada › **Executar 1 passo do agente** | Etapas verificadas avançam (6/10). Caso **R5-TP431754** detectado; `bloquear_titulo`, `abrir_incidente` e `acionar_pessoa` auditados pelo agente |
| 0:40 | Centro de Operações | Próximo corte RM20261103-MER com os títulos retidos; ferramentas usadas pelo agente |
| 0:55 | Agente › chip **"Investigue o título TP431754. Cruze os dados de contas a pagar, fornecedor e DDA…"** | Tarefa composta: ferramentas executadas (consultar_titulo, consultar_fornecedor, consultar_nf, buscar_titulos, motor de auditoria), evidências E#, inconsistências ERP × fornecedor × DDA (CNPJ 61525615000126 × raiz 20623234; DDA com boleto no banco 289), bloco **"IA: utilizada com sucesso"** com modelo gpt-6-astra, tokens e disjuntor CLOSED. Nenhuma ação nova (pedido só investigativo). Se a IA falhar, o bloco mostra a causa e o motor determinístico responde |
| 1:30 | Mostrar "Ações anteriores sobre TP431754 nesta instância" e, se houver, "negada: …" em vermelho | Ações do ciclo autônomo com horário/ator; ferramentas negadas aparecem como negadas, nunca como executadas |
| 1:45 | Jornada guiada › **Entrar como usuario_12** (Cláudia, aprovadora) › Aprovações | Solicitação `corrigir_lancamento` do TP431459 (R$ 48.380,00 → R$ 4.838,00), roteada à aprovadora correta |
| 2:00 | **Aprovar** | O agente retoma no servidor: revalida, corrige o valor, reagenda no lote RM20261105-MER; caso **R2-TP431459 → resolved** |
| 2:30 | Jornada guiada › **Avançar até o corte de 03/11 10:00** | Lote enviado sem o TP431754 → 10/10 etapas. Abrir R2-TP431459: transições, decisões e auditoria encadeada |

Falas-chave: "a IA propõe, as políticas decidem, o executor age e verifica"; "o painel mostra estimativas da
simulação, não economia comprovada".

Plano B (sem proxy): `GP_LLM_MODE=off` — o mesmo roteiro funciona; o bloco de IA diz "desligada por configuração".
