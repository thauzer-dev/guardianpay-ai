# Limitações e evolução futura

## Limitações atuais
- Dados fictícios do desafio; valores de "evitado" são estimativas da simulação, não economia comprovada.
- Identidade: só login de demonstração (`GP_DEMO_MODE=1`, localhost); provedor de identidade corporativo não integrado.
- Canais externos (e-mail, Slack, Teams) simulados por outbox interno; nenhuma mensagem real é enviada.
- A conversa aguarda a resposta da IA de forma síncrona (até 90 s) segurando a instância; `/v1/chat/jobs` não usado.
- SQLite local, um processo; atualização da interface por polling.
- Algumas ferramentas do catálogo ainda sem adaptador (bloquear_fornecedor, liberar_lote, aprovar_alteracao_bancaria,
  propor_correcao_processo, responder_colaborador); não há ferramenta de desbloqueio de título.
- Qualidade das análises do GPT-6 Astra validada em execuções pontuais (ex.: TP431754), não em avaliação sistemática.

## Evolução
1. Conectores reais (ERP, DDA/CNAB, extratos, e-mail) em modo somente leitura primeiro.
2. SSO corporativo e trilha de auditoria exportável para compliance.
3. Chamadas assíncronas ao proxy (`chat/jobs`) e fila de investigações fora do lock da instância.
4. Avaliação contínua da IA (conjunto rotulado de casos, precisão por regra, custo por caso evitado).
5. Aprendizado com feedback de aprovadores (recusas e correções alimentam limiares e regras).
6. Postgres e múltiplos workers para volume de produção.
