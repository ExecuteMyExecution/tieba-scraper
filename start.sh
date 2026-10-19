#!/usr/bin/env bash
# Git Bash / WSL 启动脚本：首次运行会创建虚拟环境并安装依赖；缺少依赖时也会自动补装。
set -Eeuo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
if [[ "$SCRIPT_PATH" != /* ]]; then
  SCRIPT_PATH="$PWD/$SCRIPT_PATH"
fi
PROJECT_ROOT="$(cd -- "${SCRIPT_PATH%/*}" && pwd -P)"
VENV_DIR="$PROJECT_ROOT/.venv"
REQUIREMENTS="$PROJECT_ROOT/requirements.txt"

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

find_python_launcher() {
  local candidate
  for candidate in python3 python py.exe py; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

VENV_PYTHON="$(find_venv_python || true)"
if [[ -z "$VENV_PYTHON" ]]; then
  PYTHON_LAUNCHER="$(find_python_launcher || true)"
  if [[ -z "$PYTHON_LAUNCHER" ]]; then
    printf '未找到 Python。请先安装 Python 3.10 或更高版本，并重新打开终端。\n' >&2
    exit 1
  fi

  if ! "$PYTHON_LAUNCHER" -m venv "$VENV_DIR"; then
    printf '虚拟环境创建失败。\n' >&2
    printf 'Ubuntu/Debian 请先安装 venv 组件：sudo apt install -y python3-venv\n' >&2
    printf '然后删除残留目录并重试：rm -rf "%s"\n' "$VENV_DIR" >&2
    exit 1
  fi

  VENV_PYTHON="$(find_venv_python || true)"
  if [[ -z "$VENV_PYTHON" ]]; then
    printf '虚拟环境创建失败，未找到 Python 解释器。\n' >&2
    exit 1
  fi
fi

# 虚拟环境可能存在但没有 pip（例如 venv 组件缺失时创建失败留下的空目录）。
if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  printf '虚拟环境缺少 pip，尝试用 ensurepip 修复...\n' >&2
  if ! "$VENV_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1; then
    printf '虚拟环境不完整：%s 存在，但缺少 pip。\n' "$VENV_DIR" >&2
    printf 'Ubuntu/Debian 请确认已安装：sudo apt install -y python3-venv\n' >&2
    printf '然后删除该目录并重试：rm -rf "%s"\n' "$VENV_DIR" >&2
    exit 1
  fi
fi

# 不只检查目录是否存在：依赖缺失（包括安装中断）时补装。
if ! "$VENV_PYTHON" -c 'import uvicorn, fastapi, aiotieba, pydantic, httpx, cv2' >/dev/null 2>&1; then
  printf '检测到依赖缺失，正在安装 requirements.txt...\n'
  "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"
fi

cd "$PROJECT_ROOT"
exec "$VENV_PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
