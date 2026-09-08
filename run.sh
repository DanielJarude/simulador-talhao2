#!/usr/bin/env bash
# ============================================================
# Orion Agro — Simulador 3D de Talhões
# Launcher: backend (FastAPI/Uvicorn) + frontend (http.server)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

# --- IDs dos processos filhos (para limpeza na saída) ---
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
    echo ""
    echo "Encerrando servidores..."
    if [ -n "$BACKEND_PID" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "$FRONTEND_PID" ]; then
        kill "$FRONTEND_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup INT TERM HUP

echo ""
echo "============================================================"
echo " Orion Agro — Simulador 3D de Talhões"
echo " Iniciando backend (FastAPI) + frontend (estático)"
echo "============================================================"
echo ""

# --- Verificar Python ---
if ! command -v python3 &>/dev/null; then
    echo "[ERRO] Python 3 não encontrado."
    echo "       Instale o Python 3.11+ e tente novamente."
    echo ""
    exit 1
fi

# --- 1. Validar .env ---
echo "[1/5] Validando arquivo de ambiente..."
if [ ! -f "$BACKEND_DIR/.env.example" ]; then
    echo "[ERRO] .env.example não encontrado na pasta backend/."
    echo "       Verifique se o repositório está completo."
    echo ""
    exit 1
fi
if [ ! -f "$BACKEND_DIR/.env" ]; then
    echo "  .env não encontrado. Copiando de .env.example..."
    if cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"; then
        echo "  .env criado com sucesso."
    else
        echo "[ERRO] Falha ao criar .env a partir do .env.example."
        echo "       Verifique as permissões da pasta backend/."
        echo ""
        exit 1
    fi
else
    echo "  .env já existe."
fi
echo ""

# --- 2. Instalar dependências (sem redundâncias) ---
echo "[2/5] Verificando dependências do backend..."
if python3 -m pip install -r "$BACKEND_DIR/requirements.txt" --quiet 2>/dev/null; then
    echo "  Dependências atualizadas."
else
    echo "[ERRO] Falha ao instalar dependências."
    echo "       Verifique a conexão com a internet e tente novamente."
    echo ""
    exit 1
fi
echo ""

# --- 3. Iniciar backend (Uvicorn) ---
echo "[3/5] Iniciando servidor FastAPI na porta 8000..."
cd "$BACKEND_DIR"
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!
cd "$SCRIPT_DIR"
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "[ERRO] Falha ao iniciar o servidor FastAPI."
    echo "       Verifique se a porta 8000 não está em uso."
    echo ""
    exit 1
fi
echo "  Backend iniciado (http://localhost:8000)."
echo ""

# --- 4. Iniciar servidor estático do frontend ---
echo "[4/5] Iniciando servidor de arquivos estáticos na porta 5501..."
python3 -m http.server 5501 &
FRONTEND_PID=$!
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo "[ERRO] Falha ao iniciar o servidor de arquivos estáticos."
    echo "       Verifique se a porta 5501 não está em uso."
    echo ""
    exit 1
fi
echo "  Frontend iniciado (http://localhost:5501)."
echo ""

# --- 5. Abrir navegador ---
echo "[5/5] Abrindo interface no navegador padrão..."
sleep 3

if command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:5501/fazendas.html"
elif command -v open &>/dev/null; then
    open "http://localhost:5501/fazendas.html"
else
    echo "  Navegador não pôde ser aberto automaticamente."
    echo "  Acesse manualmente: http://localhost:5501/fazendas.html"
fi
echo ""

echo "============================================================"
echo " Aplicação iniciada com sucesso!"
echo ""
echo "  Interface principal:  http://localhost:5501/fazendas.html"
echo "  Dashboard 3D:        http://localhost:5501/index.html"
echo "  Login / Cadastro:    http://localhost:5501/auth.html"
echo "  API Swagger:         http://localhost:8000/docs"
echo ""
echo "  Credenciais demo:    admin@orion.com / 123456"
echo ""
echo "  Pressione CTRL+C para encerrar os servidores."
echo "============================================================"
echo ""

# --- Manter o script rodando (os servidores são processos filhos) ---
wait
