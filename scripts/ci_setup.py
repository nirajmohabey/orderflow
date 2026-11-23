"""Initialize only a fresh, explicitly owned ephemeral CI PostgreSQL service."""
import json
import os
from pathlib import Path
import secrets
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT=Path(__file__).resolve().parents[1]


def main():
    if os.environ.get('CI')!='true' or os.environ.get('ODF_CI_BOOTSTRAP')!='owned-ephemeral':
        raise RuntimeError('CI setup requires an explicitly owned ephemeral service')
    url=make_url(os.environ['ODF_CI_ADMIN_URL'])
    if url.host not in {'127.0.0.1','localhost','::1'} or url.database!='postgres' or url.query or url.drivername!='postgresql+psycopg':
        raise RuntimeError('CI setup requires the explicit loopback PostgreSQL administration endpoint without routing overrides')
    if (ROOT/'.env').exists() or (ROOT/'.runtime/ci-secrets.json').exists():
        raise RuntimeError('Existing configuration was preserved; this runner requires a fresh checkout')
    kwargs=dict(host=url.host,port=url.port or 5432,user=url.username,password=url.password,connect_timeout=5)
    roles=('orderflow_app','orderflow_transform','orderflow_airflow')
    databases=('orderflow','orderflow_test','orderflow_airflow')
    passwords={role:secrets.token_urlsafe(36) for role in roles}
    with psycopg.connect(**kwargs,dbname='postgres',autocommit=True) as conn:
        if conn.execute('SELECT current_database()').fetchone()[0]!='postgres':raise RuntimeError('CI database destination mismatch')
        for role in roles:
            if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():raise RuntimeError('Existing role preserved; refusing nonfresh CI service')
        for name in databases:
            if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():raise RuntimeError('Existing database preserved; refusing nonfresh CI service')
        for role in roles:
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role),sql.Literal(passwords[role])))
        for name in databases:
            owner='orderflow_airflow' if name=='orderflow_airflow' else url.username
            conn.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name),sql.Identifier(owner)))
    with psycopg.connect(**kwargs,dbname='orderflow_airflow',autocommit=True) as conn:
        conn.execute('GRANT USAGE,CREATE ON SCHEMA public TO orderflow_airflow')
    def dsn(role,database):
        password=url.password if role==url.username else passwords[role]
        return url.set(username=role,password=password,database=database).render_as_string(hide_password=False)
    metadata=json.loads((ROOT/'fixtures/seed_metadata.json').read_text())
    values={'ODF_APP_ENV':'local','ODF_DEMO_MODE':'true','ODF_AUTH_SECRET':secrets.token_urlsafe(48),
            'ODF_JWT_ISSUER':'http://localhost:8010/fixture','ODF_JWT_AUDIENCE':'orderflow',
            'ODF_DATABASE_URL':dsn('orderflow_app','orderflow'),'ODF_ADMIN_DATABASE_URL':dsn(url.username,'orderflow'),
            'ODF_TEST_DATABASE_URL':dsn('orderflow_app','orderflow_test'),'ODF_ADMIN_TEST_DATABASE_URL':dsn(url.username,'orderflow_test'),
            'ODF_DBT_DATABASE_URL':dsn('orderflow_transform','orderflow'),'ODF_DBT_EXECUTABLE':str(ROOT/'.venv-dbt/bin/dbt'),
            'ODF_WORKER_TENANTS':','.join(t['tenant_id'] for t in metadata['tenants']),'ODF_OBJECT_ROOT':str(ROOT/'artifacts/objects')}
    runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
    (ROOT/'.env').write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf-8')
    (runtime/'ci-secrets.json').write_text(json.dumps({'airflow_database_url':dsn('orderflow_airflow','orderflow_airflow')}),encoding='utf-8')
    keys={source['source_id']:secrets.token_urlsafe(36) for source in metadata['sources']}
    (runtime/'source-keys.json').write_text(json.dumps(keys),encoding='utf-8')
    os.environ.update(values)
    from orderflow.db import migrate,seed
    migrate(values['ODF_ADMIN_DATABASE_URL'])
    with psycopg.connect(**kwargs,dbname='orderflow',autocommit=True) as conn:
        conn.execute('CREATE SCHEMA orderflow_warehouse AUTHORIZATION orderflow_transform')
    members=[{'issuer':values['ODF_JWT_ISSUER'],'subject':prefix+'-'+role,'tenant_id':tenant['tenant_id'],'role':role,'alias':prefix+'-'+role}
             for prefix,tenant in zip(('alpha','beta'),metadata['tenants'],strict=True) for role in ('operator','viewer')]
    seed(values['ODF_ADMIN_DATABASE_URL'],metadata,keys,members)
    print(json.dumps({'created_roles':list(roles),'created_databases':list(databases),'existing_data_reset':False,'credentials_printed':False}))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('CI setup stopped safely: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1) from None
