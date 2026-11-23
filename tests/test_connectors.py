"""Actual loopback HTTP, cursor recovery/CAS and immutable object contracts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread, Lock
import time

import httpx
import pytest
from sqlalchemy import text

from test_core import core, product, TENANT, CATALOG, ORDERS
from orderflow.connectors import ConnectorError, advance_cursor, catalog_app, ingest_catalog, ingest_order_events, parse_inventory, read_cursor
from orderflow.db import tenant_transaction
from orderflow.objects import LocalObjectStore, ObjectError, S3ObjectStore


@pytest.mark.parametrize('encoding',['gzip','deflate','br','identity, gzip'])
def test_upstream_content_encoding_rejected_without_reading_or_inflating(encoding):
    from orderflow.connectors import _json_request
    class NeverRead(httpx.SyncByteStream):
        touched=False
        def __iter__(self):
            self.touched=True
            raise AssertionError('Compressed upstream bytes must not be consumed')
            yield b''
    stream=NeverRead()
    def handler(request):
        assert request.headers['accept-encoding']=='identity'
        return httpx.Response(200,headers={'Content-Encoding':encoding},stream=stream)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ConnectorError,match='http_content_encoding_unsupported'):
            _json_request(client,'GET','http://owned-fixture/catalog')
    assert not stream.touched


def test_upstream_raw_byte_budget_is_checked_before_json_parse():
    from orderflow.connectors import _json_request,MAX_HTTP_BYTES
    class TooLarge(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(MAX_HTTP_BYTES//16384+1):yield b'x'*16384
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=TooLarge()))) as client:
        with pytest.raises(ConnectorError,match='http_response_too_large'):
            _json_request(client,'GET','http://owned-fixture/catalog')


def test_buffered_identity_fixture_transport_keeps_receipt_contract():
    from orderflow.connectors import _json_request
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'items':[]}))) as client:
        assert _json_request(client,'GET','http://owned-fixture/catalog')=={'items':[]}


@contextmanager
def real_http(app):
    import uvicorn
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    deadline=time.monotonic()+5
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    assert server.started,'Owned loopback server did not start'
    try:yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit=True;thread.join(5);sock.close()
        assert not thread.is_alive(),'Owned loopback server did not stop'


def test_actual_http_partial_page_process_exit_and_stable_identity_recovery(core):
    from orderflow.api import app
    records=[product('catalog-'+str(i),'P'+str(i)) for i in range(3)]
    with real_http(app) as api,real_http(catalog_app(records,2)) as catalog:
        program="""
import json,os,sys
from orderflow.connectors import ingest_catalog
p=json.loads(sys.stdin.read())
def stop(count,receipt):
 if count==1:os._exit(73)
ingest_catalog(p['tenant'],p['source'],p['key'],p['catalog'],p['api'],after_item=stop)
"""
        child=subprocess.run([sys.executable,'-c',program],input=json.dumps({'tenant':TENANT,'source':CATALOG,
            'key':core['keys'][CATALOG],'catalog':catalog,'api':api}),capture_output=True,text=True,timeout=20,env=os.environ.copy())
        assert child.returncode==73,child.stderr
        assert read_cursor(TENANT,CATALOG)==({},0)
        with tenant_transaction(TENANT) as conn:
            initial=conn.execute(text('SELECT event_id,source_event_id FROM orderflow.inbox_events')).mappings().all()
            assert len(initial)==1
        with httpx.Client(trust_env=False) as client:
            result=ingest_catalog(TENANT,CATALOG,core['keys'][CATALOG],catalog,api,client=client)
        assert result=={'pages':2,'accepted':3,'duplicates':1,'completed':True}
        cursor,revision=read_cursor(TENANT,CATALOG)
        assert cursor['completed'] and revision==2
        with tenant_transaction(TENANT) as conn:
            rows=conn.execute(text('SELECT event_id,source_event_id FROM orderflow.inbox_events')).mappings().all()
        assert len(rows)==3 and initial[0] in rows
        with httpx.Client(trust_env=False) as client:
            assert ingest_catalog(TENANT,CATALOG,core['keys'][CATALOG],catalog,api,client=client)['accepted']==0
            refreshed=ingest_catalog(TENANT,CATALOG,core['keys'][CATALOG],catalog,api,client=client,refresh=True)
        assert refreshed=={'pages':2,'accepted':3,'duplicates':3,'completed':True}
        assert read_cursor(TENANT,CATALOG)[1]==4


def test_order_event_adapter_posts_real_http_snapshots_not_additive_sales(core):
    from orderflow.api import app
    from orderflow.config import ROOT
    from orderflow.worker import drain
    records=[json.loads(line)['envelope'] for line in (ROOT/'fixtures/happy_path.jsonl').read_text(encoding='utf-8').splitlines()
             if json.loads(line)['source_id']==ORDERS]
    with real_http(app) as api,httpx.Client(trust_env=False) as client:
        assert ingest_order_events(api,ORDERS,core['keys'][ORDERS],records,client=client)=={'accepted':3,'duplicates':0}
        assert ingest_order_events(api,ORDERS,core['keys'][ORDERS],records,client=client)=={'accepted':3,'duplicates':3}
    drain([TENANT])
    with tenant_transaction(TENANT) as conn:
        assert tuple(conn.execute(text("SELECT count(*),sum((payload->>'total_minor')::bigint) FROM orderflow.current_entities WHERE entity_type='order'")).one())==(2,8000)


def test_cli_source_runs_record_actual_http_acceptance_and_pre_snapshot_failure(core,tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from orderflow.api import app
    from orderflow.config import ROOT
    from orderflow.pipeline import read_latest_report
    keys=tmp_path/'private-source-keys.json';keys.write_text(json.dumps(core['keys']),encoding='utf-8')
    failed_api=FastAPI()
    @failed_api.post('/v1/sources/{source_id}/events')
    def unavailable(source_id:str):return JSONResponse({'code':'acceptance_unavailable'},status_code=503)
    records=[product('source-run-event','SOURCE-RUN')]
    with real_http(app) as api,real_http(catalog_app(records)) as catalog,real_http(failed_api) as bad:
        base=[sys.executable,str(ROOT/'scripts/pipeline.py'),'catalog','--tenant',TENANT,'--source',CATALOG,
              '--catalog-url',catalog,'--source-keys',str(keys),'--start','2026-11-06T00:00:00Z',
              '--end','2026-11-07T00:00:00Z','--refresh']
        good=subprocess.run(base+['--api-base',api],capture_output=True,text=True,timeout=20,env=os.environ.copy())
        assert good.returncode==0,good.stderr
        run=json.loads(good.stdout)
        with tenant_transaction(TENANT) as conn:
            assert conn.execute(text('SELECT status FROM orderflow.ingestion_runs WHERE run_id=:r'),{'r':run['run_id']}).scalar_one()=='succeeded'
            assert conn.execute(text('SELECT status FROM orderflow.quality_results WHERE run_id=:r'),{'r':run['run_id']}).scalar_one()=='pass'
        failure=subprocess.run(base+['--api-base',bad],capture_output=True,text=True,timeout=20,env=os.environ.copy())
        assert failure.returncode==2
        latest=read_latest_report(TENANT)
        assert not latest['has_success'] and latest['stale'] and latest['latest_attempt']['status']=='failed'
        assert latest['latest_attempt']['failure_code']=='http_connector_failed'


def test_actual_http_commit_uncertainty_keeps_old_cursor(core):
    from fastapi import FastAPI,Request
    from fastapi.responses import JSONResponse
    from orderflow.api import app
    records=[product('uncertain-'+str(i),'Q'+str(i)) for i in range(3)]
    with real_http(app) as api,real_http(catalog_app(records,2)) as catalog:
        proxy=FastAPI();state={'calls':0,'dropped':False}
        @proxy.post('/v1/sources/{source_id}/events')
        async def forward(source_id:str,request:Request):
            with httpx.Client(trust_env=False) as client:
                response=client.post(api+'/v1/sources/'+source_id+'/events',json=await request.json(),
                                     headers={'X-Source-Key':request.headers['x-source-key']})
            state['calls']+=1
            if state['calls']==2 and not state['dropped']:
                state['dropped']=True
                assert response.status_code==202
                return JSONResponse({'code':'commit_outcome_unknown'},status_code=503)
            return JSONResponse(response.json(),status_code=response.status_code)
        with real_http(proxy) as proxied,httpx.Client(trust_env=False) as client:
            with pytest.raises(ConnectorError,match='http_acceptance_unconfirmed'):
                ingest_catalog(TENANT,CATALOG,core['keys'][CATALOG],catalog,proxied,client=client)
            assert read_cursor(TENANT,CATALOG)==({},0)
            with tenant_transaction(TENANT) as conn:
                assert conn.execute(text('SELECT count(*) FROM orderflow.inbox_events')).scalar_one()==2
            result=ingest_catalog(TENANT,CATALOG,core['keys'][CATALOG],catalog,proxied,client=client)
            assert result['accepted']==3 and result['duplicates']==2 and result['completed']


def test_cursor_compare_and_set_is_atomic_with_real_connections(core):
    body=product('cas-event')
    assert core['client'].post('/v1/sources/'+CATALOG+'/events',json=body,headers={'X-Source-Key':core['keys'][CATALOG]}).status_code==202
    def attempt(_):
        try:return advance_cursor(TENANT,CATALOG,0,{'completed':True},['cas-event'])
        except ConnectorError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sorted(map(str,results))==['1','cursor_revision_conflict']
    assert read_cursor(TENANT,CATALOG)[1]==1


@pytest.mark.parametrize('tail',[
    'SKU-1,-1\n','SKU-1,1.5\n','SKU-1,true\n','SKU-1,20\n11111111-1111-4111-8111-111111111111,INV,2026-11-03T12:00:00Z,SKU-1,20\n',
])
def test_invalid_inventory_is_rejected_before_any_artifact(tail):
    raw=('tenant_id,snapshot_id,as_of,sku,quantity\n11111111-1111-4111-8111-111111111111,INV,2026-11-03T12:00:00Z,'+tail).encode()
    with pytest.raises(ConnectorError):parse_inventory(raw)


@pytest.mark.parametrize('key',['../outside','/absolute','a//b','a/../b','a\\b','a/./b'])
def test_local_object_paths_cannot_escape_root(tmp_path,key):
    with pytest.raises(ObjectError):LocalObjectStore(tmp_path).put_immutable(key,b'data')


def test_local_immutable_atomic_publication_and_corrupt_read(tmp_path):
    store=LocalObjectStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:store.put_immutable('tenant/snapshot/hash/input.csv',b'complete'),range(8)))
    assert len({row.sha256 for row in results})==1
    with pytest.raises(ObjectError):store.put_immutable('tenant/snapshot/hash/input.csv',b'changed')
    assert store.get(results[0].key,expected_sha256=results[0].sha256)==b'complete'
    (tmp_path/results[0].key).write_bytes(b'corrupted')
    with pytest.raises(ObjectError,match='checksum'):store.get(results[0].key,expected_sha256=results[0].sha256)


def test_s3_adapter_conditional_immutable_contract_without_cloud():
    class Conflict(Exception):
        response={'ResponseMetadata':{'HTTPStatusCode':412}}
    class Client:
        def __init__(self):self.values={};self.lock=Lock()
        def put_object(self,**p):
            assert p['IfNoneMatch']=='*'
            with self.lock:
                if p['Key'] in self.values:raise Conflict()
                self.values[p['Key']]=p['Body']
        def get_object(self,**p):
            value=self.values[p['Key']]
            return {'Body':io.BytesIO(value),'ContentLength':len(value)}
    client=Client();store=S3ObjectStore(client,'synthetic-contract-bucket','owned')
    first=store.put_immutable('input/a.csv',b'original')
    assert store.put_immutable('input/a.csv',b'original')==first
    with pytest.raises(ObjectError):store.put_immutable('input/a.csv',b'changed')
    assert store.get(first.key,expected_sha256=first.sha256)==b'original'
