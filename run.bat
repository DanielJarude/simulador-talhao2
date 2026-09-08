@echo off
TITLE Orion Agro — Simulador 3D de Talhões
color 0A

echo.
echo ============================================================
echo  Orion Agro — Simulador 3D de Talhões
echo  Iniciando backend (FastAPI) + frontend (estático)
echo ============================================================
echo.

REM --- Verificar Python ---
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERRO] Python nao encontrado no PATH.
    echo        Instale o Python 3.11+ e marque "Add to PATH" na instalacao.
    echo.
    pause
    exit /b 1
)

REM --- 1. Validar .env ---
echo [1/5] Validando arquivo de ambiente...
set "BACKEND_DIR=%~dp0backend"
if not exist "%BACKEND_DIR%\.env.example" (
    echo [ERRO] .env.example nao encontrado na pasta backend\.
    echo        Verifique se o repositório está completo.
    echo.
    pause
    exit /b 1
)
if not exist "%BACKEND_DIR%\.env" (
    echo     .env nao encontrado. Copiando de .env.example...
    copy /Y "%BACKEND_DIR%\.env.example" "%BACKEND_DIR%\.env" > nul
    if %ERRORLEVEL% neq 0 (
        echo [ERRO] Falha ao criar .env a partir do .env.example.
        echo        Verifique as permissoes da pasta backend\.
        echo.
        pause
        exit /b 1
    )
    echo     .env criado com sucesso.
) else (
    echo     .env ja existe.
)
echo.

REM --- 2. Instalar dependencias (sem redundancias) ---
echo [2/5] Verificando dependencias do backend...
python -m pip install -r "%BACKEND_DIR%\requirements.txt" --quiet
if %ERRORLEVEL% neq 0 (
    echo [ERRO] Falha ao instalar dependencias.
    echo        Verifique a conexao com a internet e tente novamente.
    echo.
    pause
    exit /b 1
)
echo     Dependencias atualizadas.
echo.

REM --- 3. Iniciar backend em janela separada ---
echo [3/5] Iniciando servidor FastAPI na porta 8000...
start /D "%BACKEND_DIR%" "Orion Agro API" cmd /k "python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload"
if %ERRORLEVEL% neq 0 (
    echo [ERRO] Falha ao iniciar o servidor FastAPI.
    echo        Verifique se a porta 8000 nao esta em uso.
    echo.
    pause
    exit /b 1
)
echo     Backend iniciado (http://localhost:8000).
echo.

REM --- 4. Iniciar servidor estático do frontend em janela separada ---
echo [4/5] Iniciando servidor de arquivos estaticos na porta 5501...
start /D "%~dp0" "Orion Agro Frontend" cmd /k "python -m http.server 5501"
if %ERRORLEVEL% neq 0 (
    echo [ERRO] Falha ao iniciar o servidor de arquivos estaticos.
    echo        Verifique se a porta 5501 nao esta em uso.
    echo.
    pause
    exit /b 1
)
echo     Frontend iniciado (http://localhost:5501).
echo.

REM --- 5. Abrir navegador ---
echo [5/5] Abrindo interface no navegador padrao...
timeout /t 3 /nobreak > nul
start "" "http://localhost:5501/fazendas.html"
echo.

echo ============================================================
echo  Aplicacao iniciada com sucesso!
echo.
echo  Interface principal:  http://localhost:5501/fazendas.html
echo  Dashboard 3D:        http://localhost:5501/index.html
echo  Login / Cadastro:    http://localhost:5501/auth.html
echo  API Swagger:         http://localhost:8000/docs
echo.
echo  Credenciais demo:    admin@orion.com / 123456
echo.
echo  Para encerrar os servidores, feche as janelas
echo  "Orion Agro API" e "Orion Agro Frontend" ou use
echo  CTRL+C em cada uma delas.
echo ============================================================
echo.

pause
