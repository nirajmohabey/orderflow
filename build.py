"""Compile the frontend before Vercel packages the FastAPI/static entrypoint."""
from pathlib import Path
import shutil
import subprocess

root=Path(__file__).resolve().parent
npm=shutil.which('npm')
if npm is None: raise RuntimeError('Node.js/npm is required for the frontend build')
for arguments in (['ci'],['run','build']):
    subprocess.run([npm,*arguments],cwd=root/'dashboard',check=True)
