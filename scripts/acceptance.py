"""Independent literal-fixture oracle over real HTTP handlers and PostgreSQL.

Creates and removes only its own new database. Never resets a configured DB.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from uuid import uuid4

from dotenv import dotenv_values
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]

def main():
    values = {k:v for k,v in dotenv_values(ROOT / '.env').items() if v is not None}
    base_admin = make_url(values['ODF_ADMIN_DATABASE_URL'])
    base_runtime = make_url(values['ODF_DATABASE_URL'])
    name = 'orderflow_acceptance_' + uuid4().hex[:12]
    kwargs = {'host':base_admin.host,'port':base_admin.port,'user':base_admin.username,
              'password':base_admin.password,'connect_timeout':5}
    created = False
    results = []
    report = {'started_at':datetime.now(timezone.utc).isoformat(), 'database':name,
              'configured_database_reset':False,'scope':'In-process HTTP handlers + real PostgreSQL; literal independent fixture oracle, not hosted performance.'}
    def check(label, condition):
        results.append({'name':label,'passed':bool(condition)})
        if not condition:
            raise AssertionError(label)
    try:
        with psycopg.connect(**kwargs, dbname='postgres', autocommit=True) as conn:
            if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
                raise RuntimeError('Fresh test database unexpectedly exists')
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            created = True
        values['ODF_DATABASE_URL'] = base_runtime.set(database=name).render_as_string(hide_password=False)
        values['ODF_ADMIN_DATABASE_URL'] = base_admin.set(database=name).render_as_string(hide_password=False)
        values['ODF_AUTH_SECRET'] = secrets.token_urlsafe(48)
        values['ODF_REQUESTS_PER_MINUTE'] = '1000000'
        os.environ.update(values)
        from verification_support import validate_destination_pair
        validate_destination_pair(values['ODF_ADMIN_DATABASE_URL'],values['ODF_DATABASE_URL'],name)
        from orderflow.db import migrate, seed, dispose_engines
        metadata = json.loads((ROOT / 'fixtures/seed_metadata.json').read_text(encoding='utf-8'))
        keys = {s['source_id']:secrets.token_urlsafe(36) for s in metadata['sources']}
        members = [{'issuer':values['ODF_JWT_ISSUER'],'subject':f'{prefix}-{role}',
                    'tenant_id':tenant['tenant_id'],'role':role,'alias':f'{prefix}-{role}'}
                   for prefix,tenant in zip(('alpha','beta'),metadata['tenants'],strict=True)
                   for role in ('operator','viewer')]
        migrate(values['ODF_ADMIN_DATABASE_URL'])
        seed(values['ODF_ADMIN_DATABASE_URL'], metadata, keys, members)
        from fastapi.testclient import TestClient
        from orderflow.api import app
        from orderflow.worker import process_one
        expected = json.loads((ROOT / 'fixtures/expected_state.json').read_text(encoding='utf-8'))
        alpha, beta = [t['tenant_id'] for t in metadata['tenants']]
        identities = {}
        def observe(tenant):
            with psycopg.connect(**kwargs, dbname=name) as conn:
                observed = {}
                for table,key in [('inbox_events','distinct_inbox_events'),('processing_receipts','terminal_receipts'),('entity_revisions','entity_revisions'),('quarantines','quarantined_events')]:
                    observed[key] = conn.execute(sql.SQL('SELECT count(*) FROM orderflow.{} WHERE tenant_id=%s').format(sql.Identifier(table)),(tenant,)).fetchone()[0]
                current = conn.execute('SELECT entity_type,entity_id,entity_version,payload FROM orderflow.current_entities WHERE tenant_id=%s ORDER BY entity_type,entity_id',(tenant,)).fetchall()
                observed.update(current_products=sum(r[0]=='product' for r in current), current_orders=sum(r[0]=='order' for r in current),
                                current_order_amount_minor=sum(r[3]['total_minor'] for r in current if r[0]=='order'),
                                paid_order_amount_minor=sum(r[3]['total_minor'] for r in current if r[0]=='order' and r[3]['status']=='paid'))
                for kind,entity,version,payload in current:
                    observed[f'{kind}_{entity}_version'] = version
                    if kind == 'product':
                        observed[f'product_{entity}_unit_price_minor'] = payload['unit_price_minor']
                checkpoints = conn.execute('SELECT coalesce(sum(committed_count),0) FROM orderflow.processing_partitions WHERE tenant_id=%s',(tenant,)).fetchone()[0]
                raw_p2 = conn.execute("SELECT effective_at FROM orderflow.entity_revisions WHERE tenant_id=%s AND entity_type='product' AND entity_id='P2'",(tenant,)).fetchall()
                observed['curated_product_P2_intervals'] = len({r[0] for r in raw_p2})
                check('checkpoint equals exact terminal receipts '+tenant[:8], checkpoints == observed['terminal_receipts'])
                return observed
        def gate(key):
            truth = expected[key]
            actual = observe(truth['tenant_id'])
            for field,value in truth.items():
                if field != 'tenant_id':
                    check(key+': '+field, actual.get(field) == value)
        with TestClient(app) as client:
            for filename in expected['sequence']:
                for line in (ROOT / 'fixtures' / filename).read_text(encoding='utf-8').splitlines():
                    item = json.loads(line)
                    body = item['envelope']
                    key = (body['tenant_id'],item['source_id'],body['source_event_id'])
                    response = client.post('/v1/sources/'+item['source_id']+'/events',json=body,
                                           headers={'X-Source-Key':keys[item['source_id']]})
                    check(filename+' durable HTTP202', response.status_code == 202)
                    result = response.json()
                    if key in identities:
                        check('semantic replay original identity', result['duplicate'] and result['event_id']==identities[key])
                    else:
                        check('first identity distinct', not result['duplicate'])
                        identities[key] = result['event_id']
                for tenant in (alpha,beta):
                    for _ in range(100):
                        if process_one(tenant) is None:
                            break
                    else:
                        raise AssertionError('Fixture drain did not terminate')
                if filename == 'happy_path.jsonl': gate('after_happy_path')
                if filename == 'late_versions.jsonl': gate('after_late_versions')
                if filename == 'equal_effective_times.jsonl': gate('after_equal_effective_times')
            gate('after_all_tenant_one')
            gate('after_tenant_two')
            with psycopg.connect(**kwargs, dbname=name) as conn:
                revision_keys = set(conn.execute('SELECT tenant_id::text,entity_type,entity_id,entity_version FROM orderflow.entity_revisions').fetchall())
                literal_keys = {(alpha,'product','P1',v) for v in (1,2,3)} | {(alpha,'product','P2',v) for v in (1,2)} | {(alpha,'order','O1',v) for v in (1,2)} | {(alpha,'order','O2',1),(beta,'product','P1',1)}
                check('exact raw revision keys across both tenants',revision_keys==literal_keys)
                actual_current = {(t,k,e):(v,p) for t,k,e,v,p in conn.execute('SELECT tenant_id::text,entity_type,entity_id,entity_version,payload FROM orderflow.current_entities').fetchall()}
                literal_current = {
                    (alpha,'product','P1'):(3,{'sku':'SKU-1','name':'Notebook','unit_price_minor':1500,'currency':'USD'}),
                    (alpha,'product','P2'):(2,{'sku':'SKU-2','name':'Desk lamp','unit_price_minor':2200,'currency':'USD'}),
                    (alpha,'order','O1'):(2,{'customer_id':'C1','total_minor':3000,'currency':'USD','status':'paid'}),
                    (alpha,'order','O2'):(1,{'customer_id':'C2','total_minor':5000,'currency':'USD','status':'paid'}),
                    (beta,'product','P1'):(1,{'sku':'TENANT2-SKU','name':'Private tenant two product','unit_price_minor':700,'currency':'USD'})}
                check('exact current keys versions and payloads',actual_current==literal_current)
            tokens = {}
            for identity in ('alpha-operator','alpha-viewer','beta-operator'):
                session = client.post('/v1/dev/session',json={'identity_id':identity})
                check('fixture session '+identity,session.status_code==200)
                tokens[identity] = session.json()['access_token']
            headers = {'Authorization':'Bearer '+tokens['alpha-operator'],'X-Tenant-ID':alpha}
            beta_headers = {'Authorization':'Bearer '+tokens['beta-operator'],'X-Tenant-ID':beta}
            alpha_event = next(v for k,v in identities.items() if k[0]==alpha)
            check('cross-tenant existing event hidden',client.get('/v1/events/'+alpha_event,headers=beta_headers).status_code==404)
            check('unauthenticated event denied',client.get('/v1/events/'+alpha_event).status_code==401)
            check('viewer retry forbidden',client.post('/v1/events/'+alpha_event+'/retry',json={'reason':'Reviewed terminal event'},headers={'Authorization':'Bearer '+tokens['alpha-viewer'],'X-Tenant-ID':alpha}).status_code==403)
            check('operator cannot reopen terminal event',client.post('/v1/events/'+alpha_event+'/retry',json={'reason':'Reviewed terminal event'},headers=headers).status_code==409)
            literal_intervals = expected['product_history_intervals']
            for entity in ('P1','P2'):
                response = client.get('/v1/products/'+entity+'/history',headers=headers)
                check('history HTTP '+entity,response.status_code==200)
                actual = [{k:r[k] for k in ('entity_version','valid_from','valid_to')} for r in response.json()['intervals']]
                truth = [{'entity_version':r['version'],'valid_from':r['valid_from'],'valid_to':r['valid_to']} for r in literal_intervals if r['entity_id']==entity]
                check('literal half-open history '+entity,actual==truth)
            # Persisted snapshots are observations, never reused to derive expected truth.
            report['observed_tenants'] = {alpha:observe(alpha),beta:observe(beta)}
            report['distinct_identity_count'] = len(identities)
        report['passed'] = True
    except Exception as exc:
        report['passed'] = False
        report['failure_type'] = type(exc).__name__
        if isinstance(exc,AssertionError): report['failure'] = str(exc)
        raise
    finally:
        if 'orderflow.db' in sys.modules:
            sys.modules['orderflow.db'].dispose_engines()
        if created:
            with psycopg.connect(**kwargs, dbname='postgres',autocommit=True) as conn:
                conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            report['owned_database_removed'] = True
        report['checks'] = results
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['fixture_sha256'] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'fixtures').glob('*') if p.is_file()}
        output = ROOT / 'artifacts/checks/acceptance.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'passed':report.get('passed'), 'checks':len(results),'owned_database_removed':report.get('owned_database_removed')}))

if __name__ == '__main__':
    main()
