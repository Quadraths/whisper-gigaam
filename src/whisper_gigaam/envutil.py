"""Общие утилиты: путь к проекту и загрузка файла .env."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]


def load_env_file(path: Path) -> None:
    """Читает .env (KEY=VALUE) в окружение; уже заданные переменные не перезаписывает."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
