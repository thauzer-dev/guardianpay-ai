# GuardianPay AI — instalação rápida no Windows

Pré-requisitos: Python 3.11+ (testado em 3.13) e Node.js 18+.

```powershell
# 1) Descompacte o ZIP e entre na pasta
cd guardianpay

# 2) Backend
pip install pydantic fastapi uvicorn pytest

# 3) Frontend (gera web\dist, servido pela própria API)
cd web; npm install; npm run build; cd ..

# 4) Configuração: copie o exemplo e preencha SÓ o token (nunca versione o .env)
Copy-Item .env.example .env
#   AI_PROXY_API_KEY=<token gw-tok-... do evento>   (valor puro, sem "Bearer" e sem aspas)
#   AI_PROXY_MODEL=gpt-6-astra
#   GP_LLM_MODE=auto            (off = roda 100% determinístico, sem IA)

# 5) Validar a IA (1 geração mínima + 1 investigação real do TP431754 em banco temporário, até 3 chamadas)
$env:PYTHONPATH="src"; python -m guardianpay llm-check --tarefa

# 6) Subir a central de operações (modo demonstração, só localhost)
$env:PYTHONPATH="src"; $env:GP_DEMO_MODE="1"; python -m guardianpay.api.app
# abra http://127.0.0.1:8000
```

Testes: `$env:PYTHONPATH="src"; python -m pytest -q` (158 testes; IA simulada por mocks identificados, sem rede).

Notas
- Sem token ou com `GP_LLM_MODE=off`, tudo funciona em modo determinístico e a interface diz isso explicitamente.
- `GP_DEMO_MODE=1` habilita login simulado com identidades de teste do ERP — apenas para demonstração local.
- Os CSVs originais ficam em `data/raw` e nunca são alterados; o estado de cada instância fica em `var/instances/`.
- Limites do LLM (timeout, orçamento, disjuntor) estão documentados em `.env.example` e `docs/AI_PROXY.md`.
