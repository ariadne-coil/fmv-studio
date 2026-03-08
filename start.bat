@echo off
setlocal
set "BACKEND_ACTIVATE=.venv\Scripts\activate"
if not exist "%BACKEND_ACTIVATE%" set "BACKEND_ACTIVATE=backend\venv\Scripts\activate"

echo =========================================
echo         Starting FMV Studio API        
echo =========================================

echo Starting Backend API Server...
start "FMV Studio Backend" cmd /k "call %BACKEND_ACTIVATE% && cd backend && python -m uvicorn app.main:app --reload --port 8000"

echo Starting Frontend Next.js Server...
start "FMV Studio Frontend" cmd /k "cd frontend && npm run dev"

echo Waiting for servers to initialize...
timeout /t 5 /nobreak >nul

echo Opening FMV Studio Dashboard in your default browser...
start http://localhost:3000

echo =========================================
echo   FMV Studio is running in the background!
echo   Close the newly opened command windows 
echo   to stop the servers.
echo =========================================
