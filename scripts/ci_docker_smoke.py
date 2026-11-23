"""Build and inspect only this job's fresh, private Compose stack."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    if os.environ.get('CI')!='true':raise RuntimeError('Docker CI smoke requires the isolated CI job')
    command=['docker','compose','--project-name','orderflow-ci','--env-file','.env.docker']
    existing=subprocess.run(['docker','ps','-aq','--filter','label=com.docker.compose.project=orderflow-ci'],capture_output=True,text=True,check=True)
    volumes=subprocess.run(['docker','volume','ls','--format','{{.Name}}','--filter','label=com.docker.compose.project=orderflow-ci'],capture_output=True,text=True,check=True)
    if existing.stdout.strip() or volumes.stdout.strip():raise RuntimeError('Existing CI stack preserved; refusing a nonfresh project')
    runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'checks':[],'passed':False}
    def check(name,ok):
        report['checks'].append({'name':name,'passed':bool(ok)})
        if not ok:raise RuntimeError(name)
    started=False
    try:
        with (runtime/'ci-docker-build.log').open('wb') as output:
            started=True
            done=subprocess.run(command+['up','--build','--wait','--wait-timeout','180'],cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,timeout=900)
            check('Compose build and healthy startup',done.returncode==0)
        with httpx.Client(base_url='http://127.0.0.1:8010',timeout=10,trust_env=False) as client:
            check('container API readiness',client.get('/health/ready').status_code==200)
            check('container dashboard built index',client.get('/').status_code==200)
            check('container local favicon',client.get('/favicon.svg').status_code==200)
            session=client.post('/v1/dev/session',json={'identity_id':'alpha-operator'},headers={'Origin':'http://127.0.0.1:8010'})
            check('container same-origin fixture login',session.status_code==200)
            alpha={'Authorization':'Bearer '+session.json()['access_token']}
            overview=client.get('/v1/overview',headers=alpha).json()
            check('container latest order grain and amount',overview['counts']['orders']==2 and overview['order_totals']['current_amount_minor']==8000)
            check('container terminal business failures retained',overview['counts']['quarantined']==2)
            beta_session=client.post('/v1/dev/session',json={'identity_id':'beta-viewer'}).json()
            beta={'Authorization':'Bearer '+beta_session['access_token']}
            beta_events=client.get('/v1/events',headers=beta).json()['items']
            check('container Beta scoped inbox',len(beta_events)==1)
            check('container cross-tenant event denied',client.get('/v1/events/'+beta_events[0]['event_id'],headers=alpha).status_code==404)
        report['passed']=True
    except Exception as error:
        report['failure_type']=type(error).__name__
    finally:
        if started:
            cleanup=subprocess.run(command+['down','--volumes','--remove-orphans'],cwd=ROOT,capture_output=True,timeout=60)
            report['owned_stack_cleanup_exit_code']=cleanup.returncode
            report['passed']=report['passed'] and cleanup.returncode==0
        destination=ROOT/'artifacts/ci/docker-smoke.json';destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks'])}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
