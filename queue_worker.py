"""Vercel's file-discovered private subscriber entrypoint."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from orderflow.hosted_jobs import consume
