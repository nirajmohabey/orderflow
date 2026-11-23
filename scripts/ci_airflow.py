"""Execute actual finite Airflow/dbt work with private metadata credentials."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]


def main():
    if os.environ.get('CI')!='true' or os.environ.get('ODF_CI_BOOTSTRAP')!='owned-ephemeral':
        raise RuntimeError('This runner requires the owned CI service')
    values={k:v for k,v in dotenv_values(ROOT/'.env',interpolate=False).items() if v is not None}
    private=json.loads((ROOT/'.runtime/ci-secrets.json').read_text())
    env={**os.environ,**values,'ODF_PROJECT_ROOT':str(ROOT),'ODF_APP_PYTHON':str(ROOT/'.venv/bin/python'),
         'ODF_DBT_EXECUTABLE':str(ROOT/'.venv-dbt/bin/dbt'),'AIRFLOW_HOME':str(ROOT/'.runtime/ci-airflow'),
         'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN':private['airflow_database_url'],'AIRFLOW__CORE__LOAD_EXAMPLES':'false',
         'ODF_BATCH_TARGETS':json.dumps([{'tenant_id':'11111111-1111-4111-8111-111111111111','source_id':'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'}])}
    airflow=str(ROOT/'.venv-airflow/bin/airflow')
    runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
    log=runtime/'ci-airflow-command.log'
    commands=[[airflow,'db','migrate'],[airflow,'dags','test','orderflow_daily','2026-11-06T00:00:00+00:00',
              '--conf',json.dumps({'interval_start':'2026-11-06T00:00:00Z','interval_end':'2026-11-07T00:00:00Z'}),
              '-f',str(ROOT/'orchestration/dags/orderflow_daily.py')]]
    results=[]
    with log.open('wb') as output:
        for command in commands:
            done=subprocess.run(command,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=600)
            results.append({'operation':'metadata_migrate' if 'migrate' in command else 'finite_daily_dag','exit_code':done.returncode})
            if done.returncode:break
    os.environ.update(values)
    from orderflow.pipeline import read_latest_report
    latest=read_latest_report('11111111-1111-4111-8111-111111111111')
    passed=all(r['exit_code']==0 for r in results) and len(results)==2 and latest['has_success'] and latest['last_good']['summary']['order_total_minor']==8000
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'commands':results,'passed':passed,
            'scope':'Actual Airflow metadata migration and mapped finite DAG, actual dbt run/test and last-good publication; credentials retained only in ignored CI files.'}
    destination=ROOT/'artifacts/ci/airflow.json';destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
