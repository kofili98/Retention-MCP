# 1. Create and activate Windows virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install locked dependencies from pyproject.toml
python -m pip install --upgrade pip setuptools wheel
pip install --only-binary=:all: -e .

