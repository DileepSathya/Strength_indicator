@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate virtual environment.
    pause
    exit /b 1
)

echo Installing dependencies from requirements.txt...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

python main.py
pause
