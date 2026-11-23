"""Prove actual process death before/after commit and parallel-worker replay."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import psycopg
from verification_support import ROOT, scratch_database

def child(stage,tenant):
    from orderflow.worker import process_one
    def fault(point,event,conn):
        if point == stage:
            os._exit(73)
    process_one(tenant,fault=fault)
    raise SystemExit(74)

def main():
    checks = []
    def check(name,ok):
        checks.append({'name':name,'passed':bool(ok)})
        if not ok: raise AssertionError(name)
    report = {'generated_at':datetime.now(timezone.utc).isoformat(),'scope':'Real child os._exit(73), PostgreSQL rollback/commit, concurrent worker transactions in a new owned database.'}
    fixture = None
    try:
        with scratch_database('recovery') as fixture:
            from orderflow.auth import SourcePrincipal
            from orderflow.schemas import parse_envelope
            from orderflow.service import accept_event
            from orderflow.worker import process_one
            tenant = fixture['metadata']['tenants'][0]['tenant_id']
            source = fixture['metadata']['sources'][0]['source_id']
            principal = SourcePrincipal(tenant,source)
            template = json.loads((ROOT/'fixtures/happy_path.jsonl').read_text(encoding='utf-8').splitlines()[0])['envelope']
            def accept(index):
                body = json.loads(json.dumps(template))
                body.update(entity_id='REC-'+str(index),source_event_id='recovery-'+str(index),partition_key='product:REC-'+str(index))
                return accept_event(principal,parse_envelope(json.dumps(body).encode()))
            def snapshot(event_id):
                with psycopg.connect(**fixture['kwargs'],dbname=fixture['name']) as conn:
                    counts = [conn.execute('SELECT count(*) FROM orderflow.'+table).fetchone()[0] for table in ('entity_revisions','entity_current','processing_receipts')]
                    checkpoint = conn.execute('SELECT coalesce(sum(committed_count),0) FROM orderflow.processing_partitions').fetchone()[0]
                    event = conn.execute('SELECT status,attempt_count FROM orderflow.inbox_events WHERE event_id=%s',(event_id,)).fetchone()
                    return counts+[int(checkpoint),list(event)]
            for index,stage in enumerate(('before_materialize','after_revision','before_commit','after_commit')):
                event = accept(index)
                before = snapshot(event['event_id'])
                result = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--child',stage,'--tenant',tenant],cwd=ROOT,env=os.environ.copy(),capture_output=True,timeout=30)
                check('actual child exit73 '+stage,result.returncode==73)
                after = snapshot(event['event_id'])
                if stage != 'after_commit':
                    check('atomic rollback '+stage,after==before)
                    check('restart commits once '+stage,process_one(tenant)['status']=='completed')
                else:
                    check('after-commit receipt/current/revision/checkpoint durable',after[:4]==[v+1 for v in before[:4]] and after[4]==['completed',1])
                stable = snapshot(event['event_id'])
                check('replay cannot repeat effect '+stage,process_one(tenant) is None and snapshot(event['event_id'])==stable)
            # Same underlying worker transaction through independent connections;
            # enough partitions to test concurrent claim/exclusion behavior.
            for index in range(4,104): accept(index)
            def drain_worker():
                count = 0
                while process_one(tenant) is not None: count += 1
                return count
            with ThreadPoolExecutor(max_workers=4) as pool:
                counts = list(pool.map(lambda _:drain_worker(),range(4)))
            with psycopg.connect(**fixture['kwargs'],dbname=fixture['name']) as conn:
                observed = {table:conn.execute('SELECT count(*) FROM orderflow.'+table).fetchone()[0] for table in ('inbox_events','entity_revisions','entity_current','processing_receipts')}
                checkpoint = conn.execute('SELECT sum(committed_count) FROM orderflow.processing_partitions').fetchone()[0]
            check('four workers claim100 distinct events',sum(counts)==100)
            check('exact104 identities revisions current receipts checkpoints',set(observed.values())=={104} and checkpoint==104)
            report.update(observed=observed,worker_committed_counts=counts,database=fixture['name'],passed=True)
    except Exception as exc:
        report.update(passed=False,failure_type=type(exc).__name__)
        if isinstance(exc,AssertionError): report['failure']=str(exc)
        raise
    finally:
        report['checks']=checks
        report['owned_database_removed']=fixture is not None and fixture['removed']
        output=ROOT/'artifacts/checks/recovery.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'passed':report.get('passed'),'checks':len(checks),'owned_database_removed':report['owned_database_removed']}))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--child',choices=('before_materialize','after_revision','before_commit','after_commit'))
    parser.add_argument('--tenant')
    args=parser.parse_args()
    if args.child: child(args.child,args.tenant)
    else: main()
