"""Actual source snapshots, independent CSV checks and real dbt publication."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from test_core import core,TENANT,BETA,CATALOG,ORDERS,fixture
from orderflow.config import ROOT
from orderflow.db import get_engine,tenant_transaction
from orderflow.objects import LocalObjectStore,ObjectError
from orderflow import pipeline
from orderflow.worker import drain

CSV='cccccccc-cccc-4ccc-8ccc-cccccccccccc'
START='2026-11-06T00:00:00Z';END='2026-11-07T00:00:00Z'


@pytest.fixture
def pipeline_db(core,monkeypatch,tmp_path):
    # Transform credentials must target this fixture's fresh UUID database, never
    # the configured serving DB. No test resets any pre-existing database.
    transform=make_url(os.environ['ODF_DBT_DATABASE_URL']).set(database=make_url(core['admin']).database)
    monkeypatch.setenv('ODF_DBT_DATABASE_URL',transform.render_as_string(hide_password=False))
    monkeypatch.setenv('ODF_OBJECT_ROOT',str(tmp_path/'objects'))
    with get_engine(core['admin']).begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA orderflow_warehouse AUTHORIZATION orderflow_transform')
    return core


def complete_fixture(core):
    for filename in fixture('expected_state.json')['sequence']:
        for line in (ROOT/'fixtures'/filename).read_text(encoding='utf-8').splitlines():
            row=json.loads(line)
            response=core['client'].post('/v1/sources/'+row['source_id']+'/events',json=row['envelope'],
                                         headers={'X-Source-Key':core['keys'][row['source_id']]})
            assert response.status_code==202,response.text
        drain([TENANT,BETA])


def staged():
    return pipeline.stage_daily(TENANT,ORDERS,START,END,ROOT/'fixtures/batch_oracle.json')


def need_real_dbt():
    executable=Path(os.environ.get('ODF_DBT_EXECUTABLE',ROOT/'.venv-dbt'/('Scripts/dbt.exe' if os.name=='nt' else 'bin/dbt')))
    if not executable.exists():pytest.skip('Actual dbt executable unavailable here; required Linux DAG/dbt verification is separate, not a mock pass')


def test_snapshot_is_tenant_scoped_frozen_and_matches_literal_revision_keys(pipeline_db):
    complete_fixture(pipeline_db);run=staged()
    with tenant_transaction(TENANT,role='pipeline') as conn:
        actual={tuple(row) for row in conn.execute(text('SELECT entity_type,entity_id,entity_version FROM orderflow.pipeline_snapshot_rows WHERE run_id=:r'),{'r':run['run_id']})}
    expected={('product','P1',v) for v in (1,2,3)}|{('product','P2',v) for v in (1,2)}|{('order','O1',1),('order','O1',2),('order','O2',1)}
    assert actual==expected
    with tenant_transaction(BETA,role='viewer') as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.pipeline_snapshot_rows WHERE run_id=:r'),{'r':run['run_id']}).scalar_one()==0
    with pytest.raises(DBAPIError,match='frozen_pipeline_snapshot'):
        with tenant_transaction(TENANT,role='pipeline') as conn:
            conn.execute(text("""INSERT INTO orderflow.pipeline_snapshot_rows
                SELECT tenant_id,run_id,entity_type,'new',entity_version,source_id,event_id,effective_at,payload
                FROM orderflow.pipeline_snapshot_rows WHERE run_id=:r LIMIT 1"""),{'r':run['run_id']})


@pytest.mark.parametrize('point',['after_raw_export','after_staged_manifest'])
def test_export_failures_never_publish_and_are_visible(pipeline_db,point):
    complete_fixture(pipeline_db)
    def fault(stage,run_id):
        if stage==point:raise RuntimeError('Local owned fixture barrier')
    with pytest.raises(RuntimeError):
        pipeline.stage_daily(TENANT,ORDERS,START,END,ROOT/'fixtures/batch_oracle.json',fault=fault)
    result=pipeline.read_latest_report(TENANT)
    assert not result['has_success'] and result['stale'] and result['last_good'] is None
    assert result['latest_attempt']['status']=='failed'


def test_inventory_literal_difference_hashes_and_no_entity_side_effects(pipeline_db):
    complete_fixture(pipeline_db)
    with tenant_transaction(TENANT) as conn:
        before=conn.execute(text('SELECT count(*) FROM orderflow.entity_revisions')).scalar_one()
    report=pipeline.reconcile_inventory(TENANT,CSV,ROOT/'fixtures/inventory_snapshot.csv',ROOT/'fixtures/inventory_reference.csv')
    assert report['status']=='fail' and report['observed_total']==20 and report['reference_total']==22
    assert report['difference_total']==-2 and report['differences']==[{'sku':'SKU-1','observed_quantity':20,'reference_quantity':22,'difference':-2,'observed_present':True,'reference_present':True}]
    import hashlib
    assert report['observed_sha256']==hashlib.sha256((ROOT/'fixtures/inventory_snapshot.csv').read_bytes()).hexdigest()
    assert report['reference_sha256']==hashlib.sha256((ROOT/'fixtures/inventory_reference.csv').read_bytes()).hexdigest()
    assert pipeline.reconcile_inventory(TENANT,CSV,ROOT/'fixtures/inventory_snapshot.csv',ROOT/'fixtures/inventory_reference.csv')==report
    assert pipeline.read_inventory_latest(BETA)['latest'] is None
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.entity_revisions')).scalar_one()==before
        assert conn.execute(text('SELECT status FROM orderflow.quality_results WHERE run_id=:r'),{'r':report['run_id']}).scalar_one()=='fail'


def test_inventory_identity_conflict_and_explicit_matched_control(pipeline_db,tmp_path):
    observed=(ROOT/'fixtures/inventory_snapshot.csv').read_bytes().replace(b'INV-001',b'INV-CONTROL')
    reference=(ROOT/'fixtures/inventory_reference.csv').read_bytes().replace(b'REF-001',b'REF-CONTROL').replace(b',22',b',20')
    a,b=tmp_path/'observed-control.csv',tmp_path/'reference-control.csv';a.write_bytes(observed);b.write_bytes(reference)
    result=pipeline.reconcile_inventory(TENANT,CSV,a,b)
    assert result['status']=='pass' and result['difference_total']==0
    b.write_bytes(reference.replace(b'SKU-1,20',b'SKU-1,21'))
    with pytest.raises(pipeline.PipelineError,match='inventory_identity_conflict'):pipeline.reconcile_inventory(TENANT,CSV,a,b)
    assert pipeline.read_inventory_latest(TENANT)['latest']==result


def test_inventory_identical_concurrent_inputs_are_idempotent(pipeline_db):
    def run(_):return pipeline.reconcile_inventory(TENANT,CSV,ROOT/'fixtures/inventory_snapshot.csv',ROOT/'fixtures/inventory_reference.csv')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
    assert results[0]==results[1]
    with tenant_transaction(TENANT) as conn:assert conn.execute(text('SELECT count(*) FROM orderflow.inventory_snapshots')).scalar_one()==1


def test_inventory_missing_extra_skus_fail_even_when_total_difference_is_zero(pipeline_db,tmp_path):
    observed=(ROOT/'fixtures/inventory_snapshot.csv').read_bytes().replace(b'INV-001',b'INV-MISSING')
    reference=(ROOT/'fixtures/inventory_reference.csv').read_bytes().replace(b'REF-001',b'REF-MISSING').replace(b'SKU-1,22',b'SKU-2,20')
    a,b=tmp_path/'observed.csv',tmp_path/'reference.csv';a.write_bytes(observed);b.write_bytes(reference)
    report=pipeline.reconcile_inventory(TENANT,CSV,a,b)
    assert report['difference_total']==0 and report['status']=='fail'
    assert [(r['sku'],r['difference'],r['observed_present'],r['reference_present']) for r in report['differences']]==[
        ('SKU-1',20,True,False),('SKU-2',-20,False,True)]


def test_real_dbt_exact_order_grain_equal_time_history_and_failed_check_last_good(pipeline_db):
    need_real_dbt();complete_fixture(pipeline_db)
    result=pipeline.run_daily(TENANT,ORDERS,START,END,ROOT/'fixtures/batch_oracle.json')
    good=result['last_good'];assert result['has_success']
    assert good['summary']=={'order_count':2,'order_total_minor':8000,'currency':'USD','product_count':2,'raw_revision_count':8}
    assert [(r['entity_id'],r['entity_version'],r['total_minor']) for r in good['order_facts']]==[('O1',2,3000),('O2',1,5000)]
    assert [(r['entity_id'],r['entity_version']) for r in good['product_history']]==[('P1',1),('P1',2),('P1',3),('P2',2)]
    assert all(r['valid_to'] is None or r['valid_to']>r['valid_from'] for r in good['product_history'])
    checked=staged();pipeline.check_daily(TENANT,checked['run_id'])
    (pipeline.object_store().root/f"reports/{TENANT}/{checked['run_id']}/report_manifest.json").write_bytes(b'corrupted after checks')
    with pytest.raises(ObjectError):pipeline.publish_daily(TENANT,checked['run_id'])
    assert pipeline.read_latest_report(TENANT)['last_good']==good
    with pytest.raises(pipeline.PipelineError,match='dbt_test_failed'):
        pipeline.run_daily(TENANT,ORDERS,START,END,ROOT/'fixtures/batch_oracle.json',test_corrupt_total=True)
    after=pipeline.read_latest_report(TENANT)
    assert after['stale'] and after['last_good']==good and after['latest_attempt']['status']=='failed'
    assert any(c['status']=='fail' for c in after['latest_attempt']['checks'])


def test_real_dbt_identical_rerun_does_not_replace_publication(pipeline_db):
    need_real_dbt();complete_fixture(pipeline_db)
    first=pipeline.run_daily(TENANT,ORDERS,START,END,ROOT/'fixtures/batch_oracle.json')['last_good']
    second=pipeline.run_daily(TENANT,ORDERS,START,END,ROOT/'fixtures/batch_oracle.json')['last_good']
    assert second==first
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.report_publications')).scalar_one()==1
        assert set(conn.execute(text('SELECT status FROM orderflow.report_candidates')).scalars())=={'published','superseded'}


def test_real_dbt_empty_beta_orders_and_transform_context_isolation(pipeline_db):
    need_real_dbt();complete_fixture(pipeline_db)
    result=pipeline.run_daily(BETA,'dddddddd-dddd-4ddd-8ddd-dddddddddddd',START,END,ROOT/'fixtures/batch_oracle.json')
    assert result['last_good']['summary']=={'order_count':0,'order_total_minor':0,'currency':'USD','product_count':1,'raw_revision_count':1}
    assert result['last_good']['order_facts']==[]
    assert not pipeline.read_latest_report(TENANT)['has_success']
    with get_engine(os.environ['ODF_DBT_DATABASE_URL']).connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.pipeline_snapshot_rows')).scalar_one()==0


def test_real_dbt_delayed_older_interval_cannot_replace_newer_pointer(pipeline_db):
    need_real_dbt();complete_fixture(pipeline_db)
    old=staged();new=pipeline.stage_daily(TENANT,ORDERS,'2026-11-07T00:00:00Z','2026-11-08T00:00:00Z',ROOT/'fixtures/batch_oracle.json')
    pipeline.check_daily(TENANT,new['run_id']);latest=pipeline.publish_daily(TENANT,new['run_id'])['last_good']
    pipeline.check_daily(TENANT,old['run_id']);delayed=pipeline.publish_daily(TENANT,old['run_id'])['last_good']
    assert delayed==latest


def test_corrupt_staged_object_fails_before_dbt_and_never_publishes(pipeline_db,monkeypatch):
    complete_fixture(pipeline_db);run=staged();candidate=pipeline._candidate(TENANT,run['run_id'])
    item=candidate['manifest']['source']['objects'][0];store=pipeline.object_store()
    (store.root/item['key']).write_bytes(b'corrupted-owned-fixture')
    monkeypatch.setattr(pipeline.subprocess,'run',lambda *a,**k:pytest.fail('Corruption must fail before dbt'))
    with pytest.raises(ObjectError):pipeline.check_daily(TENANT,run['run_id'])
    report=pipeline.read_latest_report(TENANT)
    assert not report['has_success'] and report['latest_attempt']['status']=='failed'


def test_unchecked_candidate_cannot_update_last_good_pointer(pipeline_db):
    complete_fixture(pipeline_db);run=staged()
    with pytest.raises(pipeline.PipelineError,match='candidate_checks_not_passed'):pipeline.publish_daily(TENANT,run['run_id'])
    assert not pipeline.read_latest_report(TENANT)['has_success']
