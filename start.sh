#!/usr/bin/env bash
# Git Bash 启动脚本：首次运行会创建虚拟环境并安装依赖。
set -Eeuo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
if [[ "$SCRIPT_PATH" != /* ]]; then
  SCRIPT_PATH="$PWD/$SCRIPT_PATH"
fi
PROJECT_ROOT="$(cd -- "${SCRIPT_PATH%/*}" && pwd -P)"
VENV_DIR="$PROJECT_ROOT/.venv"

find_venv_python() {
  local candidate
  for candidate in "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/bin/python"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

VENV_PYTHON="$(find_venv_python || true)"
if [[ -z "$VENV_PYTHON" ]]; then
  PYTHON_LAUNCHER=""
  for candidate in python3 python py.exe py; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_LAUNCHER="$candidate"
      break
    fi
  done

  if [[ -z "$PYTHON_LAUNCHER" ]]; then
    printf '未找到 Python。请先安装 Python 3.10 或更高版本，并重新打开 Git Bash。\n' >&2
    exit 1
  fi

  "$PYTHON_LAUNCHER" -m venv "$VENV_DIR"
  VENV_PYTHON="$(find_venv_python || true)"
  if [[ -z "$VENV_PYTHON" ]]; then
    printf '虚拟环境创建失败，未找到 Python 解释器。\n' >&2
    exit 1
  fi
  "$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"
fi

cd "$PROJECT_ROOT"
exec "$VENV_PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
