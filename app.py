"""Vercel ASGI entrypoint; no database initialization at import time."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from orderflow.api import app
