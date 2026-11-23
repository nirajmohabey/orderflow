"""Own one loopback API process for the actual dashboard browser suite."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    if os.environ.get('CI')!='true':raise RuntimeError('This entrypoint is for the isolated CI job')
    folder=ROOT/'.runtime';folder.mkdir(exist_ok=True)
    with (folder/'ci-api.log').open('wb') as output:
        process=subprocess.Popen([sys.executable,'-m','uvicorn','orderflow.api:app','--host','127.0.0.1','--port','8010','--no-access-log'],cwd=ROOT,stdout=output,stderr=subprocess.STDOUT)
        try:
            with httpx.Client(timeout=2,trust_env=False) as client:
                for _ in range(100):
                    if process.poll() is not None:raise RuntimeError('Owned CI API exited')
                    try:
                        if client.get('http://127.0.0.1:8010/health/ready').status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise RuntimeError('Owned CI API readiness timed out')
            npm=shutil.which('npm.cmd') or shutil.which('npm')
            return subprocess.run([npm,'run','test:live'],cwd=ROOT/'dashboard',env={**os.environ,'ORDERFLOW_URL':'http://127.0.0.1:8010'},timeout=240).returncode
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)


if __name__=='__main__':raise SystemExit(main())
