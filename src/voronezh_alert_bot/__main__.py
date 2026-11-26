"""Точка входа для запуска через `python -m voronezh_alert_bot`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
