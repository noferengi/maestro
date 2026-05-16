@echo off
if "%1"=="" (
    echo TheMaestro Database Migration Utility (PostgreSQL only)
    echo -------------------------------------------------------
    echo Usage: .\migrate.bat ^<command^>
    echo.
    echo Commands:
    echo   status    - Show applied vs pending migrations
    echo   migrate   - Apply all pending migrations
    echo   rollback  - Revert the last migration
    echo   reset     - DESTROY all data and start fresh
    echo.
    echo Recommended Workflow:
    echo   .\migrate.bat status
    echo   .\migrate.bat migrate
    echo   .\migrate.bat status
    exit /b 1
)
D:\workspace\TheMaestro\venv\Scripts\python.exe app/migrations/runner.py %*
