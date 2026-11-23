"""Initialize new Compose fixture state; preserve existing inbox and identities."""
import json
import os
from pathlib import Path
import secrets
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

root=Path(__file__).resolve().parents[1]
url=make_url(os.environ['ODF_ADMIN_DATABASE_URL'])
kwargs={'host':url.host,'port':url.port or 5432,'user':url.username,'password':url.password,'connect_timeout':5}
with psycopg.connect(**kwargs,dbname='postgres',autocommit=True) as conn:
    for role,key in [('orderflow_app','APP_ROLE_PASSWORD'),('orderflow_transform','TRANSFORM_ROLE_PASSWORD')]:
        secret=os.environ[key]
        if len(secret)<32: raise RuntimeError('Generated role credentials required')
        if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role),sql.Literal(secret)))
    for database in ('orderflow','orderflow_test'):
        if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(database,)).fetchone():
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
from orderflow.db import migrate,seed
migrate(os.environ['ODF_ADMIN_DATABASE_URL'])
with psycopg.connect(**kwargs,dbname='orderflow',autocommit=True) as conn:
    conn.execute('CREATE SCHEMA IF NOT EXISTS orderflow_warehouse AUTHORIZATION orderflow_transform')
metadata=json.loads((root/'fixtures/seed_metadata.json').read_text(encoding='utf-8'))
keyfile=Path('/run/orderflow/source-keys.json');keyfile.parent.mkdir(parents=True,exist_ok=True)
keys=json.loads(keyfile.read_text()) if keyfile.exists() else {s['source_id']:secrets.token_urlsafe(36) for s in metadata['sources']}
keyfile.write_text(json.dumps(keys),encoding='utf-8')
members=[{'issuer':os.environ['ODF_JWT_ISSUER'],'subject':prefix+'-'+role,'tenant_id':tenant['tenant_id'],'role':role,'alias':prefix+'-'+role} for prefix,tenant in zip(('alpha','beta'),metadata['tenants'],strict=True) for role in ('operator','viewer')]
seed(os.environ['ODF_ADMIN_DATABASE_URL'],metadata,keys,members)
os.environ['ODF_SOURCE_KEYS']=str(keyfile)
from populate_demo import main as populate_demo
populate_demo()
for directory in ('/run/orderflow','/app/artifacts/objects'):
    Path(directory).mkdir(parents=True,exist_ok=True)
    os.chown(directory,10001,10001)
os.chown(keyfile,10001,10001)
print('Migrated and seeded local fixture identities; no existing event data reset; credentials omitted.')

