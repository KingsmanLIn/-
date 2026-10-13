@echo off
rem Phase 5 HTTP smoke checks against a locally running server (see
rem tests/serve_with_fake_provider.py). Usage:
rem   tests\api_smoke.cmd http://127.0.0.1:8799 smoke-token medical-2026-zang
setlocal
set BASE=%~1
set TOKEN=%~2
set PAPER=%~3
set HERE=%~dp0
set OUT=%HERE%phase5-curl-report.txt
> "%OUT%" echo # Phase 5 HTTP smoke report

call :check "GET status with a wrong token"        ""                        GET  "%BASE%/api/analysis/%PAPER%/status" wrong-token
call :check "GET status with token"               ""                        GET  "%BASE%/api/analysis/%PAPER%/status"
call :check "GET status unknown paper"            ""                        GET  "%BASE%/api/analysis/nope-nope-nope/status"
call :check "GET status traversal"                ""                        GET  "%BASE%/api/analysis/%%2e%%2e%%2flibrary/status"
call :check "GET answer when not ready"           ""                        GET  "%BASE%/api/analysis/finance-2021-zhang"
call :post  "POST generate retry"                 ""                        "%BASE%/api/analysis/%PAPER%/generate"
call :check "GET status after generate"           ""                        GET  "%BASE%/api/analysis/%PAPER%/status"
call :post  "POST generate without force"         ""                        "%BASE%/api/analysis/%PAPER%/generate"
call :post  "POST generate with force"            "force.json"              "%BASE%/api/analysis/%PAPER%/generate"
rem give the background worker a moment so the answer is ready
ping -n 6 127.0.0.1 > nul
call :check "GET answer when ready"               ""                        GET  "%BASE%/api/analysis/%PAPER%"
call :check "GET status list"                     ""                        GET  "%BASE%/api/analysis"
call :post  "POST settings with ai fields"        "settings.json"           "%BASE%/api/settings"
call :post  "POST settings with a key value"      "secret.json"             "%BASE%/api/settings"
type "%OUT%"
endlocal
exit /b 0

:check
setlocal
set TITLE=%~1
set BODY=%~2
set METHOD=%~3
set URL=%~4
set USE_TOKEN=%TOKEN%
if not "%~5"=="" set USE_TOKEN=%~5
>> "%OUT%" echo.
>> "%OUT%" echo ## %TITLE%
if "%BODY%"=="" (
  curl.exe -s -o "%TEMP%\smoke-body.txt" -w "HTTP %%{http_code} in %%{time_total}s" -X %METHOD% -H "X-Reader-Token: %USE_TOKEN%" "%URL%" >> "%OUT%"
) else (
  curl.exe -s -o "%TEMP%\smoke-body.txt" -w "HTTP %%{http_code} in %%{time_total}s" -X %METHOD% -H "X-Reader-Token: %USE_TOKEN%" -H "Content-Type: application/json" --data "@%HERE%%BODY%" "%URL%" >> "%OUT%"
)
>> "%OUT%" echo.
type "%TEMP%\smoke-body.txt" >> "%OUT%"
endlocal
exit /b 0

:post
setlocal
set TITLE=%~1
set BODY=%~2
set URL=%~3
>> "%OUT%" echo.
>> "%OUT%" echo ## %TITLE%
if "%BODY%"=="" (
  curl.exe -s -o "%TEMP%\smoke-body.txt" -w "HTTP %%{http_code} in %%{time_total}s" -X POST -H "X-Reader-Token: %TOKEN%" -H "Content-Type: application/json" --data "{}" "%URL%" >> "%OUT%"
) else (
  curl.exe -s -o "%TEMP%\smoke-body.txt" -w "HTTP %%{http_code} in %%{time_total}s" -X POST -H "X-Reader-Token: %TOKEN%" -H "Content-Type: application/json" --data "@%HERE%%BODY%" "%URL%" >> "%OUT%"
)
>> "%OUT%" echo.
type "%TEMP%\smoke-body.txt" >> "%OUT%"
endlocal
exit /b 0
