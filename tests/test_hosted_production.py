"""Bounded production auth, real RLS object/job ledger and private subscriber guards."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from vercel.queue import Message

from test_core import core, TENANT, BETA, CATALOG, product, post
from test_pipeline import pipeline_db, complete_fixture, ORDERS, CSV, START, END
from orderflow import hosted_jobs as jobs, neon_auth, pipeline
from orderflow.auth import verify_bearer
from orderflow.db import get_engine, tenant_transaction
from orderflow.objects import PostgresObjectStore, ObjectError

BASE='https://ep-unit.neonauth.c-1.us-east-1.aws.neon.tech/neondb/auth'
ORIGIN=BASE.split('/neondb')[0]


@pytest.fixture
def neon(monkeypatch):
    monkeypatch.setenv('NEON_AUTH_URL',BASE)
    monkeypatch.delenv('NEON_AUTH_JWKS_URL',raising=False)
    neon_auth._cache.clear();neon_auth._attempts.clear()
    private=Ed25519PrivateKey.generate()
    calls=[]
    monkeypatch.setattr(neon_auth,'_fetch_keys',lambda url:calls.append(url) or {'unit-key':private.public_key()})
    def token(**changes):
        now=datetime.now(timezone.utc)
        claims={'iss':ORIGIN,'aud':ORIGIN,'sub':'alpha-operator','iat':now,'exp':now+timedelta(minutes=15)}|changes
        return jwt.encode(claims,private,algorithm='EdDSA',headers={'kid':'unit-key'})
    yield SimpleNamespace(token=token,calls=calls,private=private)
    neon_auth._cache.clear();neon_auth._attempts.clear()


def test_neon_signature_cache_and_exact_claims(neon):
    assert neon_auth.decode_token(neon.token())['sub']=='alpha-operator'
    assert neon_auth.decode_token(neon.token())['iss']==ORIGIN
    assert len(neon.calls)==1


@pytest.mark.parametrize('claims',[{'aud':'foreign'},{'aud':[ORIGIN,'foreign']},{'iss':'https://foreign.invalid'},{'exp':datetime(2000,1,1,tzinfo=timezone.utc)}])
def test_neon_rejects_wrong_audience_issuer_or_expiry(neon,claims):
    with pytest.raises((ValueError,jwt.PyJWTError)):neon_auth.decode_token(neon.token(**claims))


def test_unknown_neon_key_does_not_force_unbounded_refresh(neon):
    neon_auth.decode_token(neon.token())
    token=jwt.encode({'iss':ORIGIN,'aud':ORIGIN,'sub':'alpha','iat':datetime.now(timezone.utc),'exp':datetime.now(timezone.utc)+timedelta(minutes=1)},neon.private,algorithm='EdDSA',headers={'kid':'unknown'})
    for _ in range(10):
        with pytest.raises(ValueError):neon_auth.decode_token(token)
    assert len(neon.calls)==1


def test_neon_membership_issuer_survives_every_service_recheck(core,neon,monkeypatch):
    # Distinct external subjects ensure a legacy local membership cannot make
    # this integration pass while the service accidentally uses the old issuer.
    with get_engine(core['admin']).begin() as conn:
        for subject,role in [('neon-operator','operator'),('neon-viewer','viewer')]:
            conn.execute(text('INSERT INTO orderflow.memberships(issuer,subject,tenant_id,role) VALUES(:issuer,:subject,:tenant,:role)'),
                         {'issuer':ORIGIN,'subject':subject,'tenant':TENANT,'role':role})
    client=core['client']
    operator={'Authorization':'Bearer '+neon.token(sub='neon-operator',role='viewer',tenant_id=BETA)}
    viewer={'Authorization':'Bearer '+neon.token(sub='neon-viewer',role='operator',tenant_id=BETA)}
    session=client.get('/v1/session',headers=viewer)
    assert session.status_code==200,session.text
    assert session.json()['principal']=={'tenant_id':TENANT,'subject':'neon-viewer','role':'viewer'}
    assert client.get('/v1/overview',headers=operator).status_code==200
    monkeypatch.setenv('ODF_QUEUE_ENABLED','true')
    async def send(*args,**kwargs):return 'mock-queue-message'
    monkeypatch.setattr(jobs,'send',send)
    body={'source_id':CSV,'observed_csv':'synthetic','reference_csv':'synthetic'}
    assert client.post('/v1/jobs/inventory',headers={**viewer,'Idempotency-Key':str(uuid4())},json=body).status_code==403
    accepted=client.post('/v1/jobs/inventory',headers={**operator,'Idempotency-Key':str(uuid4())},json=body)
    assert accepted.status_code==202,accepted.text
    assert client.get('/v1/session',headers={**operator,'X-Tenant-ID':BETA}).status_code==403
    with get_engine(core['admin']).begin() as conn:
        conn.execute(text('UPDATE orderflow.memberships SET active=false WHERE issuer=:issuer AND subject=:subject'),{'issuer':ORIGIN,'subject':'neon-operator'})
    assert client.get('/v1/overview',headers=operator).status_code==403


@pytest.mark.parametrize('path',['../get-session','//evil','https://evil.invalid/','%252e%252e/get-session','get-session\\evil'])
def test_neon_proxy_rejects_arbitrary_targets(neon,path):
    with pytest.raises(ValueError):neon_auth._proxy_target(path,'')


def test_neon_proxy_keeps_auth_jwt_and_makes_cookie_first_party(neon,monkeypatch):
    from fastapi.testclient import TestClient
    from orderflow.api import app
    monkeypatch.setenv('ODF_DATABASE_URL','postgresql+psycopg://unused@127.0.0.1/unused')
    monkeypatch.setenv('ODF_AUTH_SECRET','synthetic-config-secret-'*3)
    monkeypatch.setenv('ODF_APP_ENV','production');monkeypatch.setenv('ODF_DEMO_MODE','false')
    monkeypatch.setenv('ODF_ALLOWED_HOSTS','testserver');monkeypatch.setenv('ODF_ALLOWED_ORIGINS','https://testserver')
    original=httpx.AsyncClient
    observed=[]
    def upstream(request):
        observed.append(request)
        if '/reset-password/' in request.url.path:return httpx.Response(302,headers={'Location':'https://testserver/?reset_password=true&token=synthetic-reset'})
        return httpx.Response(200,json={'token':'synthetic-jwt'},headers=[('set-auth-jwt','synthetic-jwt'),('set-cookie','__Secure-better-auth.session_token=synthetic-cookie; Domain=neon.tech; Path=/neondb/auth; Secure; HttpOnly; SameSite=Lax')])
    monkeypatch.setattr(neon_auth.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(upstream),**kwargs))
    with TestClient(app,base_url='https://testserver') as client:
        config=client.get('/v1/auth/config').json()
        assert config['mode']=='neon' and config['auth_base_url']=='/auth'
        response=client.get('/auth/token',headers={'Cookie':'_vercel_jwt=protection-secret; __Secure-better-auth.session_token=auth-session; better-auth.session_data.0=chunk'})
        assert response.status_code==200 and response.headers['set-auth-jwt']=='synthetic-jwt'
        cookie=response.headers['set-cookie']
        assert 'Domain=' not in cookie and 'Path=/auth' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie
        assert str(observed[0].url)==BASE+'/token'
        assert 'protection-secret' not in observed[0].headers.get('cookie','')
        assert 'auth-session' in observed[0].headers['cookie'] and 'chunk' in observed[0].headers['cookie']
        reset=client.get('/auth/reset-password/synthetic-reset',follow_redirects=False)
        assert reset.status_code==302 and reset.headers['location'].startswith('https://testserver/?reset_password=true')
        assert client.post('/auth/sign-out',json={}).status_code==403
        assert client.post('/auth/sign-in/email',json={'callbackURL':'https://foreign.invalid'},headers={'Origin':'https://testserver'}).status_code==403
        for path in ['email-otp/request-password-reset','email-otp/reset-password','email-otp/send-verification-otp','email-otp/verify-email']:
            assert client.post('/auth/'+path,json={'email':'synthetic@example.invalid'},headers={'Origin':'https://testserver'}).status_code==200
        assert client.post('/auth/admin/create-user',json={},headers={'Origin':'https://testserver'}).status_code==404


@pytest.mark.parametrize('path',['sign-up/email','email-otp/verify-email','sign-in/email'])
def test_modern_session_cookie_attributes_survive_proxy_and_next_request(neon,monkeypatch,path):
    from fastapi.testclient import TestClient
    from orderflow.api import app
    monkeypatch.setenv('ODF_DATABASE_URL','postgresql+psycopg://unused@127.0.0.1/unused')
    monkeypatch.setenv('ODF_AUTH_SECRET','synthetic-config-secret-'*3)
    monkeypatch.setenv('ODF_APP_ENV','production');monkeypatch.setenv('ODF_DEMO_MODE','false')
    monkeypatch.setenv('ODF_ALLOWED_HOSTS','testserver');monkeypatch.setenv('ODF_ALLOWED_ORIGINS','https://testserver')
    original=httpx.AsyncClient;seen=[]
    def upstream(request):
        seen.append(request)
        if request.url.path.endswith('/get-session'):
            cookie=request.headers.get('cookie','')
            assert '__Secure-neon-auth.session_token=synthetic.signature' in cookie
            assert '__Host-neon-auth.session_data=synthetic-data' in cookie
            assert 'Priority=' not in cookie and 'Partitioned' not in cookie
            return httpx.Response(200,json={'session':{'id':'synthetic-session'}})
        return httpx.Response(200,json={'ok':True},headers=[
            ('set-cookie','__Secure-neon-auth.session_token=synthetic.signature; Domain=neon.tech; Path=/; Secure; HttpOnly; SameSite=None; Partitioned; Priority=High'),
            ('set-cookie','__Host-neon-auth.session_data=synthetic-data; Path=/; Secure; HttpOnly; SameSite=None; Partitioned')])
    monkeypatch.setattr(neon_auth.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(upstream),**kw))
    with TestClient(app,base_url='https://testserver') as client:
        response=client.post('/auth/'+path,json={'email':'synthetic@example.invalid'},headers={'Origin':'https://testserver'})
        cookies=response.headers.get_list('set-cookie')
        assert response.status_code==200 and len(cookies)==2
        assert all('Domain=' not in value and 'HttpOnly' in value and 'Secure' in value and 'SameSite=Lax' in value and 'Partitioned' in value for value in cookies)
        assert 'Path=/auth' in cookies[0] and 'Priority=High' in cookies[0]
        assert 'Path=/;' in cookies[1] and 'Path=/auth' not in cookies[1]
        session=client.get('/auth/get-session')
        assert session.status_code==200 and session.json()['session']['id']=='synthetic-session'
    assert len(seen)==2


@pytest.mark.parametrize('value',[
    'session=synthetic; Partitioned=true','session=synthetic; Priority=Urgent',
    'session=synthetic; Partitioned; partitioned','session=synthetic; unexpected=another-cookie',
])
def test_modern_cookie_support_does_not_allow_ambiguous_attributes(value):
    with pytest.raises(neon_auth.NeonAuthError):neon_auth._proxy_cookie(value)


def test_modern_host_cookie_logout_preserves_expiry():
    cookie=neon_auth._proxy_cookie('__Host-neon-auth.session_token=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Domain=neon.tech; Path=/old; Partitioned; Priority=Low')
    assert 'Domain=' not in cookie and 'Path=/;' in cookie and 'Path=/auth' not in cookie
    assert 'Max-Age=0' in cookie and 'expires=thu, 01 jan 1970 00:00:00 gmt' in cookie.lower()
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Partitioned' in cookie and 'Priority=Low' in cookie


def test_postgres_objects_are_immutable_and_tenant_scoped(core):
    store=PostgresObjectStore(TENANT)
    first=store.put_immutable('reports/sample/input.json',b'original')
    assert store.put_immutable(first.key,b'original')==first
    assert store.get(first.key,expected_sha256=first.sha256)==b'original'
    with pytest.raises(ObjectError):store.put_immutable(first.key,b'different')
    with pytest.raises(ObjectError):PostgresObjectStore(BETA).get(first.key)
    with get_engine().connect() as conn:assert conn.execute(text('SELECT count(*) FROM orderflow.stored_objects')).scalar_one()==0
    with pytest.raises(DBAPIError):
        with get_engine(core['admin']).begin() as conn:conn.execute(text("UPDATE orderflow.stored_objects SET content='changed'::bytea"))


@pytest.mark.parametrize('root,inside,outside',[
    ('C:\\owned\\objects','\\\\?\\C:\\owned\\objects\\nested\\input.csv','\\\\?\\C:\\other\\input.csv'),
    ('\\\\server\\share\\objects','\\\\?\\UNC\\server\\share\\objects\\nested\\input.csv','\\\\?\\UNC\\server\\other\\input.csv'),
])
def test_windows_extended_length_paths_preserve_physical_containment(root,inside,outside):
    from orderflow.objects import _windows_comparison_path
    assert _windows_comparison_path(inside).is_relative_to(PureWindowsPath(root))
    assert not _windows_comparison_path(outside).is_relative_to(PureWindowsPath(root))


@pytest.fixture
def queue(core,monkeypatch):
    monkeypatch.setenv('ODF_QUEUE_ENABLED','true');monkeypatch.setenv('ODF_WORKER_TENANTS',TENANT+','+BETA)
    wakes=[]
    async def send(topic,payload,**options):wakes.append(payload);return 'mock-queue-message'
    monkeypatch.setattr(jobs,'send',send)
    return core,wakes


def deliver(wake):
    asyncio.run(jobs.consume(Message(payload=jobs.Wake.model_validate(wake),metadata=None)))


def test_acceptance_transaction_and_private_subscriber_materialize_once(queue):
    core,wakes=queue
    accepted=post(core,product()).json()
    assert len(wakes)==1
    deliver(wakes[0]);deliver(wakes[0])
    assert post(core,product()).json()['duplicate'] is True
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.processing_receipts')).scalar_one()==1
        assert conn.execute(text("SELECT status FROM orderflow.hosted_jobs WHERE kind='drain'")).scalar_one()=='succeeded'


def test_new_acceptance_during_drain_is_not_lost(queue,monkeypatch):
    core,wakes=queue
    post(core,product())
    from orderflow import worker
    original=worker.drain
    added=False
    def inject(*args,**kwargs):
        nonlocal added
        result=original(*args,**kwargs)
        if not added:
            added=True
            assert post(core,product('late',entity='P2')).status_code==202
        return result
    monkeypatch.setattr(worker,'drain',inject)
    deliver(wakes[0])
    assert len(wakes)==2 and wakes[1]['generation']==1
    deliver(wakes[1])
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.processing_receipts')).scalar_one()==2
        assert conn.execute(text("SELECT status FROM orderflow.hosted_jobs WHERE kind='drain'")).scalar_one()=='succeeded'


def test_stale_job_lease_cannot_finish_or_publish(queue):
    core,wakes=queue;post(core,product())
    job=jobs._claim(jobs.Wake.model_validate(wakes[0]))
    with tenant_transaction(TENANT) as conn:
        conn.execute(text('UPDATE orderflow.hosted_jobs SET lease_token=:new WHERE tenant_id=:t AND job_id=:j'),{'new':str(uuid4()),'t':TENANT,'j':job['job_id']})
    with pytest.raises(pipeline.PipelineError,match='job_lease_lost'):jobs._finish(job,{})
    with pytest.raises(pipeline.PipelineError,match='job_lease_lost'):pipeline._publish_daily(TENANT,str(uuid4()),lease=(job['job_id'],job['lease_token']))
    with pytest.raises(pipeline.PipelineError,match='job_lease_lost'):pipeline.fail_run(TENANT,str(uuid4()),'stale_failure',lease=(job['job_id'],job['lease_token']))
    with pytest.raises(pipeline.PipelineError,match='job_lease_lost'):
        pipeline.reconcile_inventory(TENANT,CSV,pipeline.ROOT/'fixtures/inventory_snapshot.csv',pipeline.ROOT/'fixtures/inventory_reference.csv',lease=(job['job_id'],job['lease_token']))


def test_hosted_jobs_enforce_membership_scope_and_payload_identity(queue):
    core,wakes=queue;client=core['client'];key=str(uuid4())
    body={'source_id':CSV,'observed_csv':'synthetic','reference_csv':'synthetic'}
    assert client.post('/v1/jobs/inventory',headers={**core['viewer'],'Idempotency-Key':key},json=body).status_code==403
    assert client.post('/v1/jobs/inventory',headers=core['auth'],json=body).status_code==422
    headers={**core['auth'],'Idempotency-Key':key}
    accepted=client.post('/v1/jobs/inventory',headers=headers,json=body)
    assert accepted.status_code==202
    assert client.post('/v1/jobs/inventory',headers=headers,json=body).json()==accepted.json()
    assert client.post('/v1/jobs/inventory',headers=headers,json=body|{'observed_csv':'different'}).status_code==409
    assert client.get(accepted.json()['status_url'],headers=core['beta']).status_code==404
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.hosted_jobs')).scalar_one()==1


def test_inventory_private_job_retains_real_mismatch(pipeline_db,monkeypatch):
    core=pipeline_db
    monkeypatch.setenv('ODF_QUEUE_ENABLED','true');monkeypatch.setenv('ODF_WORKER_TENANTS',TENANT+','+BETA)
    monkeypatch.setenv('ODF_OBJECT_STORAGE','postgres')
    wakes=[]
    async def send(topic,payload,**options):wakes.append(payload);return 'mock-queue-message'
    monkeypatch.setattr(jobs,'send',send)
    body={'source_id':CSV,'observed_csv':(pipeline.ROOT/'fixtures/inventory_snapshot.csv').read_text(),
          'reference_csv':(pipeline.ROOT/'fixtures/inventory_reference.csv').read_text()}
    response=core['client'].post('/v1/jobs/inventory',headers={**core['auth'],'Idempotency-Key':str(uuid4())},json=body)
    assert response.status_code==202
    deliver(wakes[0]);deliver(wakes[0])
    status=core['client'].get(response.json()['status_url'],headers=core['auth']).json()
    assert status['status']=='succeeded' and status['result']['quality_passed'] is False
    assert status['result']['difference_total']==-2 and status['result']['run_id']==response.json()['job_id']
    with tenant_transaction(TENANT) as conn:assert conn.execute(text('SELECT count(*) FROM orderflow.inventory_snapshots')).scalar_one()==1


def test_daily_job_queue_runs_real_dbt_and_keeps_last_good(pipeline_db,monkeypatch):
    core=pipeline_db
    complete_fixture(core)
    monkeypatch.setenv('ODF_QUEUE_ENABLED','true');monkeypatch.setenv('ODF_WORKER_TENANTS',TENANT+','+BETA)
    monkeypatch.setenv('ODF_OBJECT_STORAGE','postgres');monkeypatch.setenv('ODF_SERVERLESS','true')
    wakes=[]
    async def send(topic,payload,**options):wakes.append(payload);return 'mock-queue-message'
    monkeypatch.setattr(jobs,'send',send)
    oracle=json.loads((pipeline.ROOT/'fixtures/batch_oracle.json').read_text())
    body={'source_id':ORDERS,'start':START,'end':END,'oracle':oracle}
    response=core['client'].post('/v1/jobs/daily',headers={**core['auth'],'Idempotency-Key':str(uuid4())},json=body)
    assert response.status_code==202,response.text
    job_id=response.json()['job_id']
    legacy_key=f'reports/{TENANT}/{job_id}/report_manifest.json'
    # An abandoned attempt may have sealed a legacy fixed key. Each queued
    # attempt must publish its own immutable manifest reference instead.
    PostgresObjectStore(TENANT).put_immutable(legacy_key,b'abandoned-attempt')
    index=0
    while index<len(wakes) and index<5:
        deliver(wakes[index]);index+=1
    status=core['client'].get(response.json()['status_url'],headers=core['auth']).json()
    assert status['status']=='succeeded',status
    good=pipeline.read_latest_report(TENANT)
    assert good['has_success'] and good['last_good']['storage']=='postgres'
    assert good['last_good']['manifest']['dbt_tests_passed']>=7
    assert good['last_good']['manifest_object']['key']!=legacy_key

    prior_hash=good['last_good']['manifest_sha256']
    changed=json.loads(json.dumps(oracle))
    alpha=next(row for row in changed['tenants'] if row['tenant_id']==TENANT)
    alpha['orders'][0]['total_minor']+=1;alpha['order_total_minor']+=1
    failed=core['client'].post('/v1/jobs/daily',headers={**core['auth'],'Idempotency-Key':str(uuid4())},json=body|{'oracle':changed})
    assert failed.status_code==202
    while index<len(wakes) and index<8:
        deliver(wakes[index]);index+=1
    failure=core['client'].get(failed.json()['status_url'],headers=core['auth']).json()
    assert failure['status']=='failed' and failure['last_error_code']=='dbt_test_failed'
    retained=pipeline.read_latest_report(TENANT)
    assert retained['stale'] and retained['latest_attempt']['status']=='failed'
    assert retained['last_good']['manifest_sha256']==prior_hash


def test_failed_dbt_diagnostic_is_bounded_and_redacts_environment_credentials(tmp_path):
    path=tmp_path/'dbt.log'
    password='synthetic-private-password';token='synthetic-private-token'
    path.write_text('x'*40000+'\nRuntime Error: read-only /var/task/warehouse/.user.yml\n'+
                    f'password={password} token={token} dsn=postgresql://user:{password}@db.invalid/test\n')
    value=pipeline._dbt_failure_diagnostic(path,'run',2,{'ODF_DBT_PASSWORD':password,'VERCEL_OIDC_TOKEN':token})
    assert len(value['log_tail'])<=32768 and 'Runtime Error: read-only' in value['log_tail']
    assert password not in value['log_tail'] and token not in value['log_tail']
    assert '[redacted]' in value['log_tail'] and all(value['project_files_present'].values())


def test_private_failed_dbt_job_retains_redacted_log_after_temporary_cleanup(pipeline_db,monkeypatch):
    core=pipeline_db;complete_fixture(core)
    monkeypatch.setenv('ODF_QUEUE_ENABLED','true');monkeypatch.setenv('ODF_WORKER_TENANTS',TENANT+','+BETA)
    monkeypatch.setenv('ODF_OBJECT_STORAGE','postgres');monkeypatch.setenv('ODF_SERVERLESS','true')
    secret='synthetic-diagnostic-test-password'
    monkeypatch.setenv('ODF_DIAGNOSTIC_TEST_PASSWORD',secret)
    wakes=[]
    async def send(topic,payload,**options):wakes.append(payload);return 'mock-queue-message'
    monkeypatch.setattr(jobs,'send',send)
    work=[]
    def failed(command,*,stdout,**kwargs):
        work.append(Path(stdout.name).parent)
        stdout.write(('Runtime Error: synthetic deployment setup failed; password='+secret+'\n').encode())
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(pipeline.subprocess,'run',failed)
    body={'source_id':ORDERS,'start':START,'end':END,'oracle':json.loads((pipeline.ROOT/'fixtures/batch_oracle.json').read_text())}
    response=core['client'].post('/v1/jobs/daily',headers={**core['auth'],'Idempotency-Key':str(uuid4())},json=body)
    assert response.status_code==202
    deliver(wakes[0]);deliver(wakes[1])
    status=core['client'].get(response.json()['status_url'],headers=core['auth']).json()
    assert status['status']=='failed' and status['last_error_code']=='dbt_run_failed'
    ref=status['result']['diagnostic']
    raw=PostgresObjectStore(TENANT).get(ref['key'],expected_sha256=ref['sha256'])
    value=json.loads(raw)
    assert value['exit_code']==2 and 'synthetic deployment setup failed' in value['log_tail']
    assert secret not in value['log_tail'] and '[redacted]' in value['log_tail']
    assert work and all(not path.exists() for path in work)


def test_proxy_rejects_browser_normalized_external_redirects():
    for value in ["/\\attacker.invalid", "%2f%5cattacker.invalid", "//attacker.invalid", "/ok%0d%0aLocation:evil"]:
        assert not neon_auth._safe_redirect(value,["https://trusted.invalid"])
    assert neon_auth._safe_redirect("/dashboard",["https://trusted.invalid"])
    assert neon_auth._safe_redirect("https://trusted.invalid/dashboard",["https://trusted.invalid"])


def test_failed_jwks_refresh_has_bounded_cooldown(neon,monkeypatch):
    calls=[]
    def failed(url):calls.append(url);raise ValueError("synthetic unavailable keys")
    monkeypatch.setattr(neon_auth,"_fetch_keys",failed)
    for _ in range(10):
        with pytest.raises(ValueError):neon_auth.decode_token(neon.token())
    assert len(calls)==1


def test_expired_job_lease_is_recovered_by_outbox(queue):
    core,wakes=queue;post(core,product())
    job=jobs._claim(jobs.Wake.model_validate(wakes[0]))
    with tenant_transaction(TENANT) as conn:
        conn.execute(text("UPDATE orderflow.hosted_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=:j"),{"j":job['job_id']})
    asyncio.run(jobs.dispatch_due(TENANT))
    assert wakes[-1]['generation']==1
    deliver(wakes[-1])
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text("SELECT status FROM orderflow.hosted_jobs WHERE job_id=:j"),{"j":job['job_id']}).scalar_one()=='succeeded'
