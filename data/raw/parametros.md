# Parâmetros de negócio: Pagamentos (contas a pagar)

Data de referência: segunda-feira, 02/11/2026, 08:00 (feriado de Finados). Período simulado: de 02/11 08:00 a 09/11 08:00; cada tique é **1 hora corrida** (168 tiques). A equipe só lança títulos em dia útil, das 08h às 18h; e-mails, WhatsApps e pedidos de colegas chegam a qualquer hora.

O Gogroup (Gocase e GoBeauté: Ápice, Barbour's, Kokeshi, Rituária, Lescent, By Samia, Auá, Yenzah) paga cerca de 131 fornecedores: fábricas de produto (FN1 a FN6 no sistema de Compras), embalagens, insumos de capa, transportadoras (Expressa Já, TransNorte, Rápido Sul, ViaLog, Correio Ágil), agências, influenciadores, aluguel, software e serviços. Novembro tem volume maior por causa das compras de Black Friday.

## Os sistemas e por que eles não conversam

| Sistema | Arquivo | Quem alimenta | Como identifica o fornecedor |
| --- | --- | --- | --- |
| ERP (cadastro e contas a pagar) | fornecedores.csv, alteracoes_cadastro.csv, contas_a_pagar.csv, agenda_remessa.csv | digitação manual da equipe; faturas de frete chegam por integração | fornecedor_id (FP + 4 dígitos), CNPJ com pontuação |
| Compras | pedidos_compra.csv | compradores | cod_fornecedor_compras (FN1 a FN6, ou FC + 4 dígitos), CNPJ só com números, nome abreviado |
| Portal NF-e | notas_fiscais.csv, nf_duplicatas.csv | XML recebido dos fornecedores | CNPJ do emitente (pode ser filial) e razão social do XML |
| Banco Meridiano | extrato_banco_meridiano.csv | banco | descrição com nome abreviado; documento = título nos pagamentos por lote |
| Banco Litoral | extrato_banco_litoral.csv | banco | histórico em texto livre com nome abreviado; sem número do título |
| Caixa de e-mail e WhatsApp da área | comunicacoes_fornecedores.jsonl | fornecedores e qualquer pessoa | remetente (e-mail ou telefone), assinatura no texto |

Ninguém digita a chave da NF no ERP: o título guarda o número da NF como cada pessoa digitou (com ou sem zeros à esquerda, às vezes com a série depois de - ou /). Uma NF parcelada vira um título por duplicata.

## Remessa bancária

| Banco pagador | Código | Fechamento do lote |
| --- | --- | --- |
| Banco Meridiano | 612 | 10:00 de cada dia útil |
| Banco Litoral | 437 | 14:00 de cada dia útil |

- `agenda_remessa.csv` é a fila: títulos agendados nos lotes de 03/11 a 13/11. No horário de fechamento o arquivo vai ao banco e o dinheiro sai. O que estiver no lote sai.
- Hoje o tesoureiro assina o lote sem conferir título por título. Títulos novos lançados durante a semana entram sozinhos no lote da data prevista.
- Lotes que fecham depois de 09/11 08:00 são avaliados pelo estado em que estiverem nessa hora.
- Pagamento feito fora do lote (PIX ou TED manual, por urgência) aparece no extrato do banco usado; a baixa no ERP é manual.

## Alçadas e aprovações

| Quem | Pode |
| --- | --- |
| analista_cp | liberar, agendar ou cancelar até R$ 5 mil; corrigir lançamento; retirar do lote; falar com o fornecedor pelo canal do cadastro |
| aprovador | aprovar até R$ 50 mil, aprovar alteração bancária, liberar lote |
| Diretora financeira (Helena Duarte, PS03) | acima de R$ 50 mil |
| Aprovação dupla (aprovador + PS01, ou PS03 acima de R$ 50 mil) | alteração bancária, pagamento sem NF, sem OC aprovada, antecipação, fornecedor com troca bancária recente sem confirmação válida |

Pessoas que o agente pode acionar (`acionar_pessoa`) estão em `politicas.json` (`acionamento_humano.pessoas`).

## Conferência de 3 vias

- **OC × NF:** a NF pode passar da OC em até 2% ou R$ 50 (o que for maior). Utilidades (energia, água, telecom, gás) não têm OC. Aluguel não tem NF (boleto do contrato).
- **NF × título:** valor do título = valor da duplicata, com tolerância de R$ 0,10 por arredondamento das parcelas. Título pago depois do vencimento pode somar multa de 2% e juros de 1% ao mês pro rata.
- **Fornecedor:** a raiz do CNPJ (8 primeiros dígitos) da NF deve ser a do fornecedor do título. Filial tem outro final de CNPJ e outra razão social na NF, e é o mesmo fornecedor.
- **Boleto:** o banco e o CNPJ do beneficiário do boleto devem bater com o fornecedor, ou com o banco de cobrança que ele sempre usou (muitos fornecedores cobram por um banco diferente do banco da conta do cadastro).

## Troca de dados bancários

Só vale com confirmação registrada por ligação para o telefone que estava no cadastro **antes** de qualquer alteração recente. Nunca para um número informado na própria mensagem, nem para um telefone trocado há menos de 30 dias. Contato com fornecedor só pelo canal do cadastro.

## Custos de referência

| Item | Valor |
| --- | --- |
| Multa por atraso | 2% do valor |
| Juros de atraso | 1% ao mês, pro rata |
| Custo de relacionamento por título atrasado | produto R$ 3.000,00, frete R$ 1.500,00, embalagens R$ 800,00, insumos_capa R$ 800,00, aluguel R$ 500,00, ti_saas R$ 400,00, utilidades R$ 400,00, servicos R$ 300,00, marketing_agencia R$ 300,00, influenciador R$ 250,00 |
| Acionamento humano desnecessário | R$ 60,00 |
| Dinheiro que saiu indevidamente | valor pago a mais, em duplicidade, a terceiro ou a golpista |

## Como o resultado é medido

Placar em R$ ao fim do período = −(dinheiro que saiu indevidamente + custo de atrasar pagamento legítimo + custo dos acionamentos humanos desnecessários). Para a referência, título legítimo que não sai no lote previsto atrasa 7 dias corridos.

## Códigos de banco usados pelos fornecedores

| Código | Banco |
| --- | --- |
| 146 | Caixa Regional de Fomento |
| 289 | Banco Horizonte |
| 368 | Banco Atlântico Sul |
| 437 | Banco Litoral |
| 551 | Banco Planalto |
| 612 | Banco Meridiano |
| 774 | Cooperativa Serrana de Crédito |
| 823 | Banco Capital Paulista |
| 905 | Nubra Pagamentos |

## Arquivos

`fornecedores.csv`, `alteracoes_cadastro.csv`, `pedidos_compra.csv`, `notas_fiscais.csv`, `nf_duplicatas.csv`, `contas_a_pagar.csv`, `agenda_remessa.csv`, `extrato_banco_meridiano.csv`, `extrato_banco_litoral.csv`, `comunicacoes_fornecedores.jsonl`, `politicas.json`, `ferramentas.json` e este documento. O significado de cada coluna está em `dicionario_de_dados.md`.
