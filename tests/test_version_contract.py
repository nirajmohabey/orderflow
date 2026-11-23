"""Terminal quarantine reserves authoritative version meaning, not just a receipt."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from threading import Event, local
import time

from sqlalchemy import text

from test_core import core, product, post, TENANT, CATALOG, ORDERS
from orderflow.auth import SourcePrincipal
from orderflow.config import ROOT
from orderflow.db import get_engine, tenant_transaction
from orderflow.schemas import Envelope
from orderflow.worker import drain, process_one


def accepted(core, body, source=CATALOG):
    response=post(core,body,source)
    assert response.status_code==202,response.text
    return response.json()['event_id']


def receipt(event_id):
    with tenant_transaction(TENANT) as conn:
        return dict(conn.execute(text('SELECT outcome,reason_code FROM orderflow.processing_receipts WHERE event_id=:e'),
                                 {'e':event_id}).mappings().one())


def invalid_version(name='bad-currency'):
    body=product(name,version=4,price=1500,effective='2026-11-04T12:00:00Z')
    body['payload']['currency']='EUR'
    return body


def test_terminal_currency_correction_requires_new_entity_version(core):
    original=invalid_version();first=accepted(core,original);drain([TENANT])
    assert receipt(first)=={'outcome':'quarantined','reason_code':'tenant_currency_mismatch'}
    corrected=deepcopy(original);corrected['source_event_id']='changed-under-v4';corrected['payload']['currency']='USD'
    conflicting=accepted(core,corrected);drain([TENANT])
    assert receipt(conflicting)=={'outcome':'quarantined','reason_code':'entity_version_conflict'}
    corrected['source_event_id']='new-correction-v5';corrected['entity_version']=5
    corrected['effective_at']=corrected['occurred_at']='2026-11-05T12:00:00Z'
    valid=accepted(core,corrected);drain([TENANT])
    assert receipt(valid)=={'outcome':'applied_current','reason_code':None}
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text("SELECT entity_version FROM orderflow.entity_revisions WHERE entity_id='P1'")).scalars().all()==[5]
        assert conn.execute(text("SELECT payload->>'currency' FROM orderflow.inbox_events WHERE event_id=:e"),{'e':first}).scalar_one()=='EUR'
        assert conn.execute(text('SELECT count(*) FROM orderflow.processing_receipts')).scalar_one()==3


def test_changed_effective_time_after_quarantine_is_version_conflict_before_currency(core):
    original=invalid_version();first=accepted(core,original);drain([TENANT])
    changed=deepcopy(original);changed['source_event_id']='same-payload-new-effective';changed['effective_at']='2026-11-05T12:00:00Z'
    second=accepted(core,changed);drain([TENANT])
    assert receipt(first)['reason_code']=='tenant_currency_mismatch'
    assert receipt(second)=={'outcome':'quarantined','reason_code':'entity_version_conflict'}
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.entity_revisions')).scalar_one()==0


def test_wrong_source_quarantine_cannot_reserve_authoritative_version(core):
    poison=product('wrong-source-meaning',price=9999)
    first=accepted(core,poison,source=ORDERS);drain([TENANT])
    assert receipt(first)=={'outcome':'quarantined','reason_code':'source_not_authoritative'}
    valid=product('authoritative-meaning',price=1000)
    second=accepted(core,valid);drain([TENANT])
    assert receipt(second)=={'outcome':'applied_current','reason_code':None}
    with tenant_transaction(TENANT) as conn:
        assert tuple(conn.execute(text("SELECT source_id::text,entity_version,(payload->>'unit_price_minor')::bigint FROM orderflow.current_entities WHERE entity_id='P1'")).one())==(CATALOG,1,1000)


def test_identical_invalid_meaning_stays_quarantined_and_valid_duplicate_is_allowed(core):
    invalid=invalid_version();first=accepted(core,invalid)
    again=deepcopy(invalid);again['source_event_id']='identical-invalid-new-event';second=accepted(core,again)
    drain([TENANT])
    assert receipt(first)==receipt(second)=={'outcome':'quarantined','reason_code':'tenant_currency_mismatch'}
    valid=product('valid-first',entity='VALID')
    third=accepted(core,valid);drain([TENANT])
    duplicate=deepcopy(valid);duplicate['source_event_id']='valid-identical-new-event'
    fourth=accepted(core,duplicate);drain([TENANT])
    assert receipt(third)['outcome']=='applied_current' and receipt(fourth)['outcome']=='duplicate_revision'
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT count(*) FROM orderflow.entity_revisions')).scalar_one()==1
        assert conn.execute(text('SELECT count(*) FROM orderflow.processing_receipts')).scalar_one()==4
        assert conn.execute(text('SELECT sum(committed_count) FROM orderflow.processing_partitions')).scalar_one()==4


def test_new_claim_rule_retains_consistent_lower_late_revision(core):
    first=accepted(core,product('late-v1',version=1,price=1000,effective='2026-11-01T12:00:00Z'));drain([TENANT])
    newest=accepted(core,product('late-v3',version=3,price=1500,effective='2026-11-03T12:00:00Z'));drain([TENANT])
    middle=accepted(core,product('late-v2',version=2,price=1200,effective='2026-11-02T12:00:00Z'));drain([TENANT])
    assert receipt(first)['outcome']==receipt(newest)['outcome']=='applied_current'
    assert receipt(middle)=={'outcome':'history_only','reason_code':None}
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text('SELECT entity_version FROM orderflow.entity_revisions ORDER BY entity_version')).scalars().all()==[1,2,3]
        assert tuple(conn.execute(text("SELECT entity_version,(payload->>'unit_price_minor')::bigint FROM orderflow.current_entities")).one())==(3,1500)


def test_uncommitted_first_meaning_serializes_later_acceptance(core,monkeypatch):
    from orderflow import service
    # Commit the empty partition first: contention must be on its service-held
    # row lock, rather than a first-time partition INSERT's unique constraint.
    with tenant_transaction(TENANT) as conn:
        conn.execute(text("INSERT INTO orderflow.processing_partitions(tenant_id,source_id,partition_key) VALUES(:t,:s,'product:P1')"),{'t':TENANT,'s':CATALOG})
    original=service.tenant_transaction
    labels=local();first_inserted=Event();second_started=Event();release=Event();pids={}
    @contextmanager
    def observed_transaction(*args,**kwargs):
        with original(*args,**kwargs) as conn:
            label=labels.value
            pids[label]=conn.execute(text('SELECT pg_backend_pid()')).scalar_one()
            if label=='second':second_started.set()
            yield conn
            if label=='first':
                first_inserted.set()
                assert release.wait(8),'Owned before-commit barrier was not released'
    monkeypatch.setattr(service,'tenant_transaction',observed_transaction)
    invalid=invalid_version('first-uncommitted')
    changed=deepcopy(invalid);changed['source_event_id']='second-would-redefine';changed['payload']['currency']='USD'
    def accept(label,body):
        labels.value=label
        return service.accept_event(SourcePrincipal(TENANT,CATALOG),Envelope.model_validate(body))
    blockers=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first=pool.submit(accept,'first',invalid)
            assert first_inserted.wait(3)
            second=pool.submit(accept,'second',changed)
            assert second_started.wait(3)
            deadline=time.monotonic()+2
            with get_engine(core['admin']).connect() as observer:
                while time.monotonic()<deadline:
                    blockers=observer.execute(text('SELECT pg_blocking_pids(:pid)'),{'pid':pids['second']}).scalar_one()
                    if pids['first'] in blockers:break
                    time.sleep(.01)
            assert pids['first'] in blockers,'Later intake did not wait for the first authoritative partition claim'
            assert not second.done()
            assert process_one(TENANT) is None,'No uncommitted identity may materialize'
            release.set()
            a=first.result(timeout=4);b=second.result(timeout=4)
        finally:release.set()
    monkeypatch.setattr(service,'tenant_transaction',original)
    drain([TENANT])
    assert receipt(a['event_id'])=={'outcome':'quarantined','reason_code':'tenant_currency_mismatch'}
    assert receipt(b['event_id'])=={'outcome':'quarantined','reason_code':'entity_version_conflict'}
    with tenant_transaction(TENANT) as conn:
        rows=conn.execute(text('SELECT source_event_id,inbox_sequence FROM orderflow.inbox_events ORDER BY inbox_sequence')).all()
        assert [row[0] for row in rows]==['first-uncommitted','second-would-redefine']
        assert conn.execute(text('SELECT count(*) FROM orderflow.entity_revisions')).scalar_one()==0
        assert conn.execute(text('SELECT sum(committed_count) FROM orderflow.processing_partitions')).scalar_one()==2
    proof={'scope':'Fresh owned test DB; actual independent PostgreSQL connections, no serving reset',
           'first_backend_pid':pids['first'],'second_backend_pid':pids['second'],'observed_blockers':blockers,
           'first_sequence':rows[0][1],'second_sequence':rows[1][1],'terminal_outcomes':['tenant_currency_mismatch','entity_version_conflict'],
           'revision_count':0,'receipt_and_checkpoint_count':2,'passed':True}
    path=ROOT/'artifacts/version-claim-race.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
