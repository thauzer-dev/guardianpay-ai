# GuardianPay AI — Resumo executivo

**O que é.** Um colega digital de Contas a Pagar que monitora pagamentos, cruza evidências entre sistemas, identifica
inconsistências antes das remessas, executa contenções autorizadas e solicita aprovação humana quando necessário.

**Problema.** Em contas a pagar, fraudes e erros (boleto com beneficiário trocado, duplicidade, valor acima da
duplicata, troca bancária não confirmada, título já pago) passam porque a evidência está espalhada: ERP, cadastro de
fornecedores, NF-e, DDA, extratos e e-mails. A conferência manual corre contra o horário de corte do lote bancário —
quando alguém percebe, o dinheiro já saiu.

**Solução.** Agentes que operam continuamente sobre o relógio da operação:
1. **Observam** eventos e o estado dos lotes;
2. **Investigam** com 7 regras de auditoria cruzando os 24 CSVs do desafio, e com o GPT-6 Astra (AI Proxy) para
   raciocínio contextual — que só lê por ferramentas do catálogo e só cita evidências existentes;
3. **Decidem** por tabela determinística + proposta da IA validada;
4. **Agem** apenas por ferramentas, sempre passando pelo PolicyEngine (papéis, alçadas, corte do lote, aprovação dupla);
5. **Verificam** relendo o estado operacional e registram tudo em auditoria com hash encadeado;
6. **Escalam** para a pessoa certa e **retomam** sozinhos depois da aprovação.

**Valor demonstrado (dados fictícios do desafio).**
- Boleto falso do TP431754 (CNPJ do beneficiário com raiz diferente do fornecedor; DDA com o boleto legítimo):
  bloqueado antes do corte, com incidente aberto e pessoas acionadas — o lote sai sem ele.
- TP431459 com valor 10× acima da duplicata (R$ 48.380 × R$ 4.838): retirado do lote, correção roteada para a
  aprovadora correta; após aprovar, o agente revalida, corrige, reagenda e encerra o caso.
- Investigação composta real com GPT-6 Astra validada no ambiente do time (2 chamadas, 5.314 tokens, nenhuma mutação
  em pedido apenas investigativo, ferramentas não autorizadas bloqueadas).
- Valores de "evitado" no painel são **estimativas da simulação** sobre dados fictícios, não economia comprovada.

**Por que não é um chatbot.** Observação operacional contínua · investigação contextual · uso real de ferramentas ·
execução persistente no servidor · políticas e autorização · verificação das ações · continuidade após aprovação ·
auditoria encadeada. A IA nunca concede permissão, escolhe identidade, aprova ou executa sem o executor + políticas.

**Viabilidade.** Python + SQLite + FastAPI + React, sem framework de agentes; roda num notebook; funciona sem IA
(fallback determinístico explícito); 158 testes automatizados; IA com orçamento, timeout e disjuntor por instância.
