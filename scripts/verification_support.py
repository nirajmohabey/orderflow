"""Fresh owned databases for verification runners; no reset operation exists."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import re
from uuid import uuid4
from dotenv import dotenv_values
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


def validate_connection_url(value):
    """Reject ambiguous routing before creating or restoring any database."""
    parsed = make_url(value)
    forbidden = {'dbname','database','host','hostaddr','port','user','password','service','servicefile','options'}
    if parsed.drivername != 'postgresql+psycopg' or not parsed.host or not parsed.database:
        raise RuntimeError('Verification requires an explicit PostgreSQL destination')
    if forbidden.intersection(k.lower() for k in parsed.query):
        raise RuntimeError('Connection-routing overrides are forbidden in verification URLs')
    return parsed


def report_output(default):
    path = Path(os.environ.get('ODF_VERIFICATION_OUTPUT', default))
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to((ROOT / 'artifacts').resolve()):
        raise RuntimeError('Verification output must stay inside project artifacts')
    return path

def validate_destination_pair(admin_url, runtime_url, expected):
    """Verify connected identities before any verification migration or write."""
    if not re.fullmatch(r'orderflow_(?:acceptance|recovery|benchmark|backup|oracle|restore)_[0-9a-f]{12}',expected):
        raise RuntimeError('Unrecognized owned verification database')
    # Validate both strings before the first connection.
    for value in (admin_url,runtime_url):
        validate_connection_url(value)
    destinations=[]
    for index,url in enumerate((admin_url,runtime_url)):
        engine=create_engine(url,pool_pre_ping=True,connect_args={'connect_timeout':5})
        try:
            with engine.connect() as conn:
                identity=conn.execute(text('SELECT current_database(),inet_server_addr()::text,inet_server_port()')).one()
                if identity[0]!=expected:
                    raise RuntimeError('Connected verification database differs from owned target')
                if index==1:
                    unsafe=conn.execute(text('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user')).scalar_one()
                    if unsafe: raise RuntimeError('Runtime verification role must enforce tenant policies')
                destinations.append(tuple(identity))
        finally: engine.dispose()
    if destinations[0]!=destinations[1]:
        raise RuntimeError('Administrative and runtime verification destinations differ')
    return destinations[0]

@contextmanager
def scratch_database(purpose):
    if purpose not in {'recovery','benchmark','backup','oracle'}:
        raise ValueError('Unrecognized verification purpose')
    original = dict(os.environ)
    env = {k:v for k,v in dotenv_values(ROOT / '.env').items() if v is not None}
    admin = validate_connection_url(env['ODF_ADMIN_DATABASE_URL'])
    runtime = validate_connection_url(env['ODF_DATABASE_URL'])
    if env.get('ODF_DBT_DATABASE_URL'):
        validate_connection_url(env['ODF_DBT_DATABASE_URL'])
    name = 'orderflow_'+purpose+'_'+uuid4().hex[:12]
    kwargs = dict(host=admin.host,port=admin.port,user=admin.username,password=admin.password,connect_timeout=5)
    created = False
    fixture = None
    try:
        with psycopg.connect(**kwargs,dbname='postgres',autocommit=True) as conn:
            if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
                raise RuntimeError('Owned verification identity already exists')
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            created = True
        env['ODF_DATABASE_URL'] = runtime.set(database=name).render_as_string(hide_password=False)
        env['ODF_ADMIN_DATABASE_URL'] = admin.set(database=name).render_as_string(hide_password=False)
        if env.get('ODF_DBT_DATABASE_URL'):
            env['ODF_DBT_DATABASE_URL'] = make_url(env['ODF_DBT_DATABASE_URL']).set(database=name).render_as_string(hide_password=False)
        env['ODF_AUTH_SECRET'] = secrets.token_urlsafe(48)
        env['ODF_REQUESTS_PER_MINUTE'] = '1000000'
        os.environ.update(env)
        validate_destination_pair(env['ODF_ADMIN_DATABASE_URL'],env['ODF_DATABASE_URL'],name)
        from orderflow.db import migrate, seed
        metadata = json.loads((ROOT/'fixtures/seed_metadata.json').read_text(encoding='utf-8'))
        keys = {s['source_id']:secrets.token_urlsafe(36) for s in metadata['sources']}
        members = [{'issuer':env['ODF_JWT_ISSUER'],'subject':prefix+'-'+role,'tenant_id':tenant['tenant_id'],'role':role,'alias':prefix+'-'+role}
                   for prefix,tenant in zip(('alpha','beta'),metadata['tenants'],strict=True) for role in ('operator','viewer')]
        migrate(env['ODF_ADMIN_DATABASE_URL'])
        seed(env['ODF_ADMIN_DATABASE_URL'],metadata,keys,members)
        with psycopg.connect(**kwargs,dbname=name,autocommit=True) as conn:
            if conn.execute('SELECT current_database()').fetchone()[0] != name:
                raise RuntimeError('Connected verification database identity mismatch')
        fixture = {'name':name,'env':env,'kwargs':kwargs,'metadata':metadata,'source_keys':keys,'removed':False}
        yield fixture
    finally:
        from orderflow.db import dispose_engines
        dispose_engines()
        if created:
            with psycopg.connect(**kwargs,dbname='postgres',autocommit=True) as conn:
                conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            if fixture: fixture['removed'] = True
        os.environ.clear()
        os.environ.update(original)
