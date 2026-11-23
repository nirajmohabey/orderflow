"""Start one local component and record the actual interpreter's ownership."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import UUID
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--component',choices=('api','worker'),required=True)
    parser.add_argument('--instance-id',required=True)
    args=parser.parse_args()
    instance=str(UUID(args.instance_id))
    load_dotenv(ROOT/'.env',override=False)
    os.chdir(ROOT)
    from orderflow.config import Settings
    if Settings.from_env().app_env!='local':
        raise RuntimeError('Use the deployment process manager outside local mode')
    target=ROOT/'.runtime'/(args.component+'.process.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    record={'pid':os.getpid(),'root':str(ROOT),'script':str(Path(__file__).resolve()),
            'component':args.component,'instance_id':instance,'started_at':datetime.now(timezone.utc).isoformat()}
    target.write_text(json.dumps(record),encoding='utf-8')
    try:
        if args.component=='api':
            import uvicorn
            uvicorn.run('orderflow.api:app',host='127.0.0.1',port=8010,access_log=False)
        else:
            from orderflow.worker import main as worker_main
            sys.argv=[sys.argv[0]]
            worker_main()
    finally:
        if target.exists() and json.loads(target.read_text(encoding='utf-8')).get('instance_id')==instance:
            target.unlink()

if __name__=='__main__':main()
