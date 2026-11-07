# Pitch (~3 min)

**[0:00 — Problema]** Toda empresa paga centenas de boletos por dia. O analista de contas a pagar confere ERP, nota
fiscal, DDA, extrato e e-mail — sistemas que não conversam — correndo contra o horário de corte do banco. Um boleto
com o beneficiário trocado parece igual a qualquer outro. Quando alguém percebe, o dinheiro já saiu.

**[0:30 — Solução]** O GuardianPay é um colega digital de Contas a Pagar. Ele não espera perguntas: acompanha o relógio
da operação, cruza as evidências dos sistemas antes de cada remessa, e quando algo não fecha, age — dentro das regras.

**[0:50 — Como funciona]** Agentes especializados — auditoria financeira, risco e fraude, tesouraria, colaboração com
aprovadores — coordenados por um orquestrador. Sete regras de auditoria encontram o problema; o GPT-6 Astra, pelo
AI Proxy, aprofunda a investigação, pede leituras adicionais e explica as evidências. Mas a IA não tem a chave do
cofre: toda ação passa por ferramentas, por um motor de políticas com papéis e alçadas, é verificada relendo o estado
e fica numa auditoria encadeada por hash.

**[1:20 — Demonstração 1]** TP431754: o boleto paga um CNPJ de outra raiz; o cadastro do fornecedor e o DDA apontam o
boleto legítimo em outro banco. O agente bloqueia o título antes do corte, abre incidente e aciona as pessoas certas.
No Agent Workspace, a investigação mostra quais ferramentas rodaram, quais evidências sustentam cada conclusão, se a IA
foi usada — e quais ações foram negadas pelas políticas.

**[2:00 — Demonstração 2]** TP431459: valor dez vezes acima da duplicata. O agente tira o título do lote e pede a
correção à aprovadora correta. Ela aprova; o agente retoma sozinho, revalida, corrige, reagenda e fecha o caso.

**[2:30 — Valor e cultura]** Menos fraude e erro saindo pela porta, sem atrasar quem é legítimo, com rastreabilidade
para auditoria. Construímos iterando com feedback real: quando a IA falhava no ambiente do time, investigamos a causa,
medimos e corrigimos com disjuntor, orçamento e diagnóstico transparentes — e o sistema sempre diz a verdade sobre o que
a IA fez ou não fez.

**[2:55 — Fecho]** GuardianPay: autonomia com responsabilidade em contas a pagar.
