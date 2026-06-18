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

echo [OK] Running with Administrator privileges.
echo.

:: ── Step 1: Create cert_store folder ──
echo [1] Creating cert_store directory...
if not exist "cert_store" mkdir cert_store
echo [OK] cert_store is ready.
echo.

:: ── Step 2: Check for winget ──
echo [2] Checking for Winget (Windows Package Manager)...
where winget >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Winget not found. Please install Winget from:
    echo     https://github.com/microsoft/winget-cli/releases
    pause
    exit /b
)
echo [OK] Winget found.
echo.

:: ── Step 3: Install mkcert (if missing) ──
echo [3] Checking for mkcert...
where mkcert >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] mkcert not found. Installing via Winget...
    winget install --id FiloSottile.mkcert --accept-package-agreements --silent
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install mkcert.
        pause
        exit /b
    )
    echo [OK] mkcert installed.

    :: ── Refresh PATH so mkcert is recognized immediately ──
    echo [INFO] Refreshing system PATH...
    for /f "usebackq tokens=2,*" %%A in (`reg query HKCU\Environment /v PATH ^| findstr /i PATH`) do set "USER_PATH=%%B"
    for /f "usebackq tokens=2,*" %%A in (`reg query HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment /v PATH ^| findstr /i PATH`) do set "SYS_PATH=%%B"
    set "PATH=%SYS_PATH%;%USER_PATH%;%PATH%"
) else (
    echo [OK] mkcert found.
)
echo.

:: ── Step 4: Install Local CA ──
echo [4] Installing Local Certificate Authority...
mkcert -install
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Local CA.
    pause
    exit /b
)
echo [OK] Local CA installed.
echo.

:: ── Step 5: Generate certificates ──
echo [5] Generating certificates for localhost and 127.0.0.1...
mkcert localhost 127.0.0.1
if %errorlevel% neq 0 (
    echo [ERROR] Failed to generate certificates.
    pause
    exit /b
)
echo [OK] Certificates generated.
echo.

:: ── Step 6: Move to cert_store ──
echo [6] Moving certificates to cert_store...
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