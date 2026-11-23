"""A finite daily workflow. The continuous inbox worker is a separate process."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from airflow.sdk import dag, task, get_current_context

PROJECT=Path(os.environ.get('ODF_PROJECT_ROOT',Path(__file__).resolve().parents[2])).resolve()
APP_PYTHON=os.environ.get('ODF_APP_PYTHON',str(PROJECT/'.venv/bin/python'))
# Only explicit, deployment-reviewed tenant/source pairs are scheduled.
TARGETS=json.loads(os.environ.get('ODF_BATCH_TARGETS','[]'))
for target in TARGETS:
    UUID(target['tenant_id']);UUID(target['source_id'])
    if set(target)-{'tenant_id','source_id','catalog_source_id','catalog_url','api_base','order_source_id','order_event_file'}:
        raise ValueError('Batch targets contain unsupported fields; never place credentials in target JSON')
    for name in ['catalog_source_id','order_source_id']:
        if name in target:UUID(target[name])


def invoke(operation,*arguments):
    command=[APP_PYTHON,str(PROJECT/'scripts/pipeline.py'),operation,*map(str,arguments)]
    result=subprocess.run(command,cwd=PROJECT,capture_output=True,text=True,timeout=420)
    if result.returncode:
        # Pipeline CLI emits a redacted error code, never a connection URL.
        raise RuntimeError('OrderFlow '+operation+' failed: '+result.stderr[-1000:])
    if len(result.stdout.encode('utf-8'))>1024*1024:raise RuntimeError('Oversized pipeline result')
    return json.loads(result.stdout)


def interval_bounds(context):
    start,end=context['data_interval_start'],context['data_interval_end']
    if start==end:
        conf=context['dag_run'].conf or {}
        if not isinstance(conf.get('interval_start'),str) or not isinstance(conf.get('interval_end'),str):
            raise RuntimeError('Manual runs require interval_start and interval_end in DagRun conf')
        start=datetime.fromisoformat(conf['interval_start'].replace('Z','+00:00'))
        end=datetime.fromisoformat(conf['interval_end'].replace('Z','+00:00'))
    if start.tzinfo is None or end.tzinfo is None:
        raise RuntimeError('A fixed UTC interval is required')
    start,end=start.astimezone(timezone.utc),end.astimezone(timezone.utc)
    if end-start!=timedelta(days=1) or start.time()!=datetime.min.time():
        raise RuntimeError('An exact 24-hour UTC day is required')
    return start.isoformat(),end.isoformat()


@dag(dag_id='orderflow_daily',schedule='@daily',start_date=datetime(2026,11,1,tzinfo=timezone.utc),
     catchup=False,max_active_runs=1,tags=['orderflow','finite-batch'])
def daily():
    @task(retries=0)
    def configured_targets():
        if not TARGETS or len(TARGETS)>10:
            raise RuntimeError('Configure 1–10 explicit tenant/source batch targets')
        return TARGETS

    @task(retries=0)
    def ingest(target):
        start,end=interval_bounds(get_current_context())
        if target.get('catalog_url'):
            if not target.get('catalog_source_id') or not target.get('api_base'):
                raise RuntimeError('Catalog ingestion needs explicit source and API base')
            invoke('catalog','--tenant',target['tenant_id'],'--source',target['catalog_source_id'],
                   '--catalog-url',target['catalog_url'],'--api-base',target['api_base'],'--refresh','--start',start,'--end',end)
        if target.get('order_event_file'):
            if not target.get('api_base'):
                raise RuntimeError('Order ingestion needs an explicit API base')
            invoke('orders','--tenant',target['tenant_id'],'--source',target.get('order_source_id',target['source_id']),
                   '--api-base',target['api_base'],'--input',target['order_event_file'],'--start',start,'--end',end)
        # Source HTTP202 is durable acceptance, not proof of materialization.
        # A finite worker drain is a separate CLI operation before snapshot.
        if target.get('catalog_url') or target.get('order_event_file'):
            invoke('drain','--tenant',target['tenant_id'])
        return {'tenant_id':target['tenant_id'],'source_id':target['source_id']}

    @task(retries=0)
    def stage(target):
        context=get_current_context()
        start,end=interval_bounds(context)
        return invoke('stage','--tenant',target['tenant_id'],'--source',target['source_id'],
                      '--start',start,'--end',end,
                      '--oracle',os.environ.get('ODF_BATCH_ORACLE',str(PROJECT/'fixtures/batch_oracle.json')))

    @task(retries=0)
    def checks(run):
        args=['--tenant',run['tenant_id'],'--run-id',run['run_id']]
        if os.environ.get('ODF_TEST_CORRUPT_TOTAL')=='true':args.append('--test-corrupt-total')
        return invoke('check',*args)

    @task(retries=0)
    def publish(run):
        value=invoke('publish','--tenant',run['tenant_id'],'--run-id',run['run_id'])
        return {'tenant_id':run['tenant_id'],'attempt_run_id':run['run_id'],
                'published_run_id':value['last_good']['run_id'] if value['last_good'] else None,
                'has_success':value['has_success'],'stale':value['stale']}

    publish.expand(run=checks.expand(run=stage.expand(target=ingest.expand(target=configured_targets()))))


orderflow_daily=daily()
