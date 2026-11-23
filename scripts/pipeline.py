"""Finite local batch/connector administration; never prints credentials."""
import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='operation',required=True)
    for name in ['stage','daily']:
        p=commands.add_parser(name);p.add_argument('--tenant',required=True);p.add_argument('--source',required=True)
        p.add_argument('--start',required=True);p.add_argument('--end',required=True)
        p.add_argument('--oracle',default=str(ROOT/'fixtures/batch_oracle.json'))
    for name in ['check','publish']:
        p=commands.add_parser(name);p.add_argument('--tenant',required=True);p.add_argument('--run-id',required=True)
    p=commands.add_parser('inventory');p.add_argument('--tenant',required=True);p.add_argument('--source',required=True)
    p.add_argument('--observed',required=True);p.add_argument('--reference',required=True)
    p=commands.add_parser('drain');p.add_argument('--tenant',required=True)
    for name in ['catalog','orders']:
        p=commands.add_parser(name);p.add_argument('--tenant',required=True);p.add_argument('--source',required=True)
        p.add_argument('--api-base',default='http://127.0.0.1:8010');p.add_argument('--source-keys',default=str(ROOT/'.runtime/source-keys.json'))
        p.add_argument('--start');p.add_argument('--end')
        if name=='catalog':
            p.add_argument('--catalog-url',default='http://127.0.0.1:8015');p.add_argument('--refresh',action='store_true')
        else:p.add_argument('--input',required=True)
    for p in commands.choices.values():
        p.add_argument('--test-fail-after',choices=['after_raw_export','after_staged_manifest','after_dbt_run','after_dbt_test','before_pointer_commit','after_pointer_commit'])
        p.add_argument('--test-corrupt-total',action='store_true')
    args=parser.parse_args()
    from orderflow import pipeline
    from orderflow.db import primitive
    if (args.test_fail_after or args.test_corrupt_total) and os.environ.get('ODF_APP_ENV')!='local':
        parser.error('Test faults require the explicitly local environment')
    def fault(stage,run_id):
        if args.test_fail_after==stage:raise pipeline.PipelineError('injected_'+stage)
    source_run=None
    try:
        common={'fault':fault if args.test_fail_after else None}
        if args.operation in {'stage','daily'}:
            function=pipeline.stage_daily if args.operation=='stage' else pipeline.run_daily
            if args.operation=='daily':common['test_corrupt_total']=args.test_corrupt_total
            value=function(args.tenant,args.source,args.start,args.end,args.oracle,**common)
        elif args.operation=='check':value=pipeline.check_daily(args.tenant,args.run_id,test_corrupt_total=args.test_corrupt_total,**common)
        elif args.operation=='publish':value=pipeline.publish_daily(args.tenant,args.run_id,**common)
        elif args.operation=='inventory':value=pipeline.reconcile_inventory(args.tenant,args.source,args.observed,args.reference)
        elif args.operation=='drain':
            from orderflow.worker import drain
            from orderflow.db import tenant_transaction
            from sqlalchemy import text
            value=drain([args.tenant],max_events=100000,deadline_seconds=120)
            with tenant_transaction(args.tenant) as conn:
                pending=conn.execute(text("SELECT count(*) FROM orderflow.inbox_events WHERE status IN ('pending','retry_wait')")).scalar_one()
            if pending:raise pipeline.PipelineError('backlog_not_drained')
        else:
            from orderflow.connectors import ingest_catalog,ingest_order_events
            secret=json.loads(Path(args.source_keys).read_text(encoding='utf-8'))[args.source]
            if bool(args.start)!=bool(args.end):raise pipeline.PipelineError('both_source_interval_bounds_required')
            if args.start:source_run=pipeline.start_run(args.tenant,args.source,args.start,args.end)
            if args.operation=='catalog':value=ingest_catalog(args.tenant,args.source,secret,args.catalog_url,args.api_base,refresh=args.refresh)
            else:
                def records():
                    with Path(args.input).open(encoding='utf-8') as source:
                        for line in iter(lambda:source.readline(65537),''):
                            if len(line.encode('utf-8'))>65536:raise pipeline.PipelineError('event_line_too_large')
                            if line.strip():
                                row=json.loads(line)
                                if row.get('source_id',args.source)==args.source:yield row.get('envelope',row)
                value=ingest_order_events(args.api_base,args.source,secret,records())
            if source_run:
                pipeline.finish_source_run(args.tenant,source_run,value['accepted']);value['run_id']=source_run
        print(json.dumps(primitive(value),separators=(',',':')))
        return 0
    except Exception as error:
        if source_run:pipeline.fail_run(args.tenant,source_run,'http_connector_failed')
        print(json.dumps({'status':'failed','code':getattr(error,'code',type(error).__name__)}),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
