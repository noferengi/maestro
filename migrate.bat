@echo off
if "%1"=="" (
    echo TheMaestro Database Migration Utility (PostgreSQL only)
    echo -------------------------------------------------------
    echo Usage: .\migrate.bat ^<command^>
    echo.
    echo Commands:
    echo   migrate   - Apply pending migrations: TEST DB first, then PROD DB
    echo   status    - Show applied vs pending for both databases
    echo   rollback  - Revert the last migration on both databases
    echo   reset     - DESTROY all data on both databases and re-migrate (Dev only)
    echo   test      - Apply pending migrations to the TEST database only
    echo   prod      - Apply pending migrations to the PROD database only
    echo.
    echo Recommended Workflow:
    echo   .\migrate.bat status
    echo   .\migrate.bat migrate
    echo   .\migrate.bat status
    exit /b 1
)
D:\workspace\TheMaestro\venv\Scripts\python.exe app/migrations/runner.py %*
