"""Vercel's supported FastAPI entrypoint; no migration or worker side effects."""
from orderflow.api import app

__all__=['app']
