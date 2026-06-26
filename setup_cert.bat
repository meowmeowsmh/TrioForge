@echo off
title Ollama Chat - SSL Certificate Setup
echo ============================================================
echo   🧠 Ollama Custom Chat - SSL Certificate Setup
echo ============================================================
echo.

:: ── Check for Administrator privileges ──
NET SESSION >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] This script requires Administrator privileges.
    echo     Restarting with elevated permissions...
    timeout /t 2 /nobreak >nul
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
echo [OK] Administrator privileges granted.
echo.

:: ── Step 1: Create cert_store folder ──
echo [1] Creating cert_store directory...
if not exist "cert_store" mkdir cert_store
echo [OK] cert_store is ready.
echo.

:: ── Step 2: Check for mkcert (download if missing) ──
echo [2] Checking for mkcert...
where mkcert >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] mkcert not found. Downloading directly...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-v1.4.4-windows-amd64.exe' -OutFile '%TEMP%\mkcert.exe' -UseBasicParsing"
    if exist "%TEMP%\mkcert.exe" (
        move "%TEMP%\mkcert.exe" "%SystemRoot%\System32\mkcert.exe" >nul
        echo [OK] mkcert installed to System32.
    ) else (
        echo [ERROR] Failed to download mkcert. Please check your internet connection.
        pause
        exit /b
    )
) else (
    echo [OK] mkcert found.
)
echo.

:: ── Step 3: Install Local CA ──
echo [3] Installing Local Certificate Authority...
mkcert -install
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Local CA.
    pause
    exit /b
)
echo [OK] Local CA installed.
echo.

:: ── Step 4: Generate certificates ──
echo [4] Generating certificates for localhost and 127.0.0.1...
mkcert localhost 127.0.0.1
if %errorlevel% neq 0 (
    echo [ERROR] Failed to generate certificates.
    pause
    exit /b
)
echo [OK] Certificates generated.
echo.

:: ── Step 5: Move to cert_store ──
echo [5] Moving certificates to cert_store...
if exist "localhost+1.pem" move "localhost+1.pem" "cert_store\" >nul
if exist "localhost+1-key.pem" move "localhost+1-key.pem" "cert_store\" >nul
echo [OK] Certificates moved.
echo.

echo ============================================================
echo   ✅ Setup Complete!
echo ============================================================
echo.
echo   📂 Certificates are in: .\cert_store\
echo   🚀 You can now run: python app.py
echo   🌐 Open: https://localhost:5000
echo.
pause