"""Provision this checkout's private loopback PostgreSQL cluster without resets."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from urllib.parse import quote

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
PORT = 55449

def executable(name):
    found = shutil.which(name)
    candidate = Path(os.environ.get('PG_BIN', r'C:\Program Files\PostgreSQL\17\bin')) / (name + ('.exe' if os.name == 'nt' else ''))
    if found:
        return found
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError(f'{name} is missing; install PostgreSQL17+ command-line tools or use Docker Compose')

def run(arguments):
    # Windows PostgreSQL's detached helper inherits standard handles. A pipe
    # would keep communicate() waiting after pg_ctl itself has exited.
    with (RUNTIME / 'bootstrap-command.log').open('ab') as output:
        result = subprocess.run(arguments, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError(f'{Path(arguments[0]).name} failed; private runtime logs contain diagnostics')
    return result.returncode

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provision-only', action='store_true')
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    data = (RUNTIME / 'postgres').resolve()
    marker = RUNTIME / 'cluster.json'
    credentials = RUNTIME / 'local-credentials.json'
    envfile = ROOT / '.env'
    if data.exists() and not marker.exists():
        raise RuntimeError('Existing unrecognized PostgreSQL data directory preserved; no changes made')
    if marker.exists():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved != {'data_directory': str(data), 'port': PORT, 'project_root': str(ROOT)}:
            raise RuntimeError('Cluster ownership marker mismatch; no changes made')
    else:
        marker.write_text(json.dumps({'data_directory':str(data),'port':PORT,'project_root':str(ROOT)}), encoding='utf-8')
    if credentials.exists():
        keys = json.loads(credentials.read_text(encoding='utf-8'))
    else:
        if data.exists() or envfile.exists():
            raise RuntimeError('Existing configuration preserved; missing local credential registry')
        keys = {name:secrets.token_urlsafe(36) for name in ('admin','app','transform','jwt')}
        credentials.write_text(json.dumps(keys), encoding='utf-8')
    if not (data / 'PG_VERSION').exists():
        password_file = RUNTIME / 'init-password'
        password_file.write_text(keys['admin'], encoding='utf-8')
        try:
            run([executable('initdb'), '-D', str(data), '-U', 'postgres', '--encoding=UTF8', '--locale=C',
                 '--auth-local=trust', '--auth-host=scram-sha-256', '--pwfile='+str(password_file)])
        finally:
            password_file.unlink(missing_ok=True)
    status = subprocess.run([executable('pg_ctl'), '-D', str(data), 'status'], capture_output=True)
    if status.returncode:
        run([executable('pg_ctl'), '-D', str(data), '-l', str(RUNTIME / 'postgres.log'), '-w',
             '-o', f'-h 127.0.0.1 -p {PORT}', 'start'])
    connection = {'host':'127.0.0.1','port':PORT,'user':'postgres','password':keys['admin'],'connect_timeout':5}
    with psycopg.connect(**connection, dbname='postgres', autocommit=True) as conn:
        actual = conn.execute('SELECT inet_server_port(), current_user').fetchone()
        if actual != (PORT, 'postgres'):
            raise RuntimeError('Connected cluster identity mismatch')
        for role, secret in [('orderflow_app',keys['app']), ('orderflow_transform',keys['transform'])]:
            if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role),sql.Literal(secret)))
        for name in ('orderflow','orderflow_test'):
            if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
                conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    metadata = json.loads((ROOT / 'fixtures/seed_metadata.json').read_text(encoding='utf-8'))
    issuer = 'http://localhost:8010/fixture'
    values = {'ODF_APP_ENV':'local','ODF_DEMO_MODE':'true','ODF_AUTH_SECRET':keys['jwt'],
              'ODF_JWT_ISSUER':issuer,'ODF_JWT_AUDIENCE':'orderflow',
              'ODF_WORKER_TENANTS':','.join(t['tenant_id'] for t in metadata['tenants']),
              'ODF_OBJECT_ROOT':str(ROOT / 'artifacts/objects')}
    def url(role,password,database):
        return f'postgresql+psycopg://{role}:{quote(password)}@127.0.0.1:{PORT}/{database}'
    values.update({'ODF_DATABASE_URL':url('orderflow_app',keys['app'],'orderflow'),
                   'ODF_ADMIN_DATABASE_URL':url('postgres',keys['admin'],'orderflow'),
                   'ODF_TEST_DATABASE_URL':url('orderflow_app',keys['app'],'orderflow_test'),
                   'ODF_ADMIN_TEST_DATABASE_URL':url('postgres',keys['admin'],'orderflow_test'),
                   'ODF_DBT_DATABASE_URL':url('orderflow_transform',keys['transform'],'orderflow')})
    if envfile.exists():
        from dotenv import dotenv_values
        current = dotenv_values(envfile)
        if any(current.get(k) != v for k,v in values.items()):
            raise RuntimeError('Existing .env differs from this local cluster; preserved without overwrite')
    else:
        envfile.write_text('\n'.join(f'{k}={v}' for k,v in values.items())+'\n', encoding='utf-8')
    source_key_file = RUNTIME / 'source-keys.json'
    if source_key_file.exists():
        source_keys = json.loads(source_key_file.read_text(encoding='utf-8'))
    else:
        source_keys = {s['source_id']:secrets.token_urlsafe(36) for s in metadata['sources']}
        source_key_file.write_text(json.dumps(source_keys), encoding='utf-8')
    if not args.provision_only:
        os.environ.update(values)
        from orderflow.db import migrate, seed
        migrate(values['ODF_ADMIN_DATABASE_URL'])
        with psycopg.connect(**connection, dbname='orderflow', autocommit=True) as conn:
            conn.execute('CREATE SCHEMA IF NOT EXISTS orderflow_warehouse AUTHORIZATION orderflow_transform')
        memberships = []
        for prefix,tenant in zip(('alpha','beta'), metadata['tenants'], strict=True):
            for role in ('operator','viewer'):
                alias = prefix+'-'+role
                memberships.append({'issuer':issuer,'subject':alias,'tenant_id':tenant['tenant_id'],'role':role,'alias':alias})
        seed(values['ODF_ADMIN_DATABASE_URL'], metadata, source_keys, memberships)
    result = {'generated_at':datetime.now(timezone.utc).isoformat(),'host':'127.0.0.1','port':PORT,
              'databases':['orderflow','orderflow_test'],'reset_existing_data':False,
              'phase':'provisioned' if args.provision_only else 'migrated_and_seeded',
              'source_key_file':'.runtime/source-keys.json','credentials_printed':False}
    output = ROOT / 'artifacts/checks/bootstrap.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result,indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Bootstrap stopped: {type(exc).__name__}; configuration and existing databases were preserved.', file=sys.stderr)
        raise SystemExit(1) from None
