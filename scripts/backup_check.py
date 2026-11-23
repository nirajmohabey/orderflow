"""Dump one exported snapshot, restore a new owned database, verify all rows."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4
from dotenv import dotenv_values
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT=Path(__file__).resolve().parents[1]

def binary(name):
    found=shutil.which(name)
    candidate=Path(os.environ.get('PG_BIN',r'C:\Program Files\PostgreSQL\17\bin'))/(name+('.exe' if os.name=='nt' else ''))
    if found: return found
    if candidate.exists(): return str(candidate)
    raise RuntimeError('PostgreSQL backup command-line tools are required')

def digest_rows(conn):
    tables=conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='orderflow' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
    result={}
    for (table,) in tables:
        rows=conn.execute(sql.SQL('SELECT row_to_json(r) FROM orderflow.{} r').format(sql.Identifier(table))).fetchall()
        canonical=sorted(json.dumps(r[0],sort_keys=True,separators=(',',':'),ensure_ascii=False) for r in rows)
        result[table]={'rows':len(rows),'sha256':hashlib.sha256('\n'.join(canonical).encode()).hexdigest()}
    return result

def main():
    values={k:v for k,v in dotenv_values(ROOT/'.env').items() if v is not None}
    url=make_url(values['ODF_ADMIN_DATABASE_URL'])
    kwargs=dict(host=url.host,port=url.port,user=url.username,password=url.password,connect_timeout=5)
    target='orderflow_restore_'+uuid4().hex[:12]
    if target==url.database: raise RuntimeError('Restore destination must be new')
    private=ROOT/'.runtime/backups';private.mkdir(parents=True,exist_ok=True)
    dump=private/(target+'.dump')
    env=os.environ.copy();env['PGPASSWORD']=url.password
    options=['--host',url.host,'--port',str(url.port or 5432),'--username',url.username]
    created=False
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'source_database':url.database,'restore_database':target,'source_database_written':False,'passed':False}
    try:
        with psycopg.connect(**kwargs,dbname=url.database) as source:
            source.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            snapshot=source.execute('SELECT pg_export_snapshot()').fetchone()[0]
            baseline=digest_rows(source)
            result=subprocess.run([binary('pg_dump'),*options,'--dbname',url.database,'--format=custom','--file',str(dump),'--snapshot',snapshot],env=env,capture_output=True,timeout=90)
            if result.returncode: raise RuntimeError('pg_dump failed')
        with psycopg.connect(**kwargs,dbname='postgres',autocommit=True) as admin:
            if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s',(target,)).fetchone(): raise RuntimeError('Restore target exists')
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
            created=True
        result=subprocess.run([binary('pg_restore'),*options,'--dbname',target,'--exit-on-error','--no-owner',str(dump)],env=env,capture_output=True,timeout=90)
        if result.returncode: raise RuntimeError('pg_restore failed')
        with psycopg.connect(**kwargs,dbname=target) as restored:
            actual=digest_rows(restored)
        if baseline!=actual: raise AssertionError('Restored all-table hashes differ from the exact exported snapshot')
        # Authenticate and replay the original identity against the restored DB.
        # Terminal receipts must keep the original state, without new effects.
        overrides=dict(values)
        overrides['ODF_DATABASE_URL']=make_url(values['ODF_DATABASE_URL']).set(database=target).render_as_string(hide_password=False)
        overrides['ODF_ADMIN_DATABASE_URL']=url.set(database=target).render_as_string(hide_password=False)
        from verification_support import validate_destination_pair
        validate_destination_pair(overrides['ODF_ADMIN_DATABASE_URL'],overrides['ODF_DATABASE_URL'],target)
        os.environ.update(overrides)
        from orderflow.auth import authenticate_source
        from orderflow.schemas import parse_envelope
        from orderflow.service import accept_event
        from orderflow.worker import drain
        keys=json.loads((ROOT/'.runtime/source-keys.json').read_text(encoding='utf-8'))
        truth=json.loads((ROOT/'fixtures/expected_state.json').read_text(encoding='utf-8'))
        duplicate_count=0
        for filename in truth['sequence']:
            for line in (ROOT/'fixtures'/filename).read_text(encoding='utf-8').splitlines():
                item=json.loads(line)
                response=accept_event(authenticate_source(keys[item['source_id']],item['source_id']),parse_envelope(json.dumps(item['envelope']).encode()))
                if not response['duplicate']: raise AssertionError('Restore replay created a new identity')
                duplicate_count+=1
        tenant_ids=json.loads((ROOT/'fixtures/seed_metadata.json').read_text(encoding='utf-8'))['tenants']
        processed=drain([t['tenant_id'] for t in tenant_ids])['processed']
        with psycopg.connect(**kwargs,dbname=target) as restored:
            after=digest_rows(restored)
        # Authentication rate budgets may move; business/audit/pointer state may not.
        comparison={k:v for k,v in baseline.items() if k!='request_budgets'}
        if comparison!={k:v for k,v in after.items() if k!='request_budgets'} or processed:
            raise AssertionError('Restore replay changed business state')
        report.update(passed=True,table_hashes=baseline,table_count=len(baseline),duplicate_replays=duplicate_count,
                      new_worker_effects=processed,dump_sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
                      external_objects='Database report JSON and stored manifest hashes verified. This test does not restore a remote object store.')
    finally:
        try:
            from orderflow.db import dispose_engines
            dispose_engines()
        except ImportError: pass
        if created:
            with psycopg.connect(**kwargs,dbname='postgres',autocommit=True) as admin:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(target)))
            report['owned_restore_removed']=True
        output=ROOT/'artifacts/checks/backup-restore.json';output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({k:report.get(k) for k in ('passed','table_count','duplicate_replays','new_worker_effects','owned_restore_removed')}))

if __name__=='__main__': main()
