"""Native private Vercel Queues over a durable PostgreSQL job/outbox ledger."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from vercel.queue import DuplicateIdempotencyKeyError, Message, RetryAfter, send, subscribe

from .config import Settings
from .db import primitive, tenant_transaction

TOPIC="orderflow-jobs"
log=logging.getLogger("orderflow.jobs")


def enabled():
    return os.environ.get("ODF_QUEUE_ENABLED", "false").lower()=="true"


def _payload(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)


def enqueue_drain(conn, tenant_id):
    """Called inside the acceptance/retry transaction, so no wakeup can be lost."""
    if not enabled():return
    job=str(uuid5(NAMESPACE_URL,"orderflow:drain:"+str(tenant_id)))
    empty="{}";digest=hashlib.sha256(empty.encode()).hexdigest()
    conn.execute(text("""INSERT INTO orderflow.hosted_jobs(tenant_id,job_id,kind,payload,payload_sha256)
        VALUES(:t,:j,'drain','{}',:h) ON CONFLICT(tenant_id,job_id) DO UPDATE
        SET status=CASE WHEN orderflow.hosted_jobs.status IN('succeeded','failed') THEN 'pending' ELSE orderflow.hosted_jobs.status END,
            generation=orderflow.hosted_jobs.generation+CASE WHEN orderflow.hosted_jobs.status IN('succeeded','failed') THEN 1 ELSE 0 END,
            phase='start',available_at=LEAST(orderflow.hosted_jobs.available_at,clock_timestamp()),
            attempts=CASE WHEN orderflow.hosted_jobs.status IN('succeeded','failed') THEN 0 ELSE orderflow.hosted_jobs.attempts END,
            last_error_code=NULL,updated_at=clock_timestamp()"""),{"t":str(tenant_id),"j":job,"h":digest})


def enqueue_request(principal,kind,payload,key):
    from .service import DomainError, _member
    if not enabled():raise DomainError(503,"jobs_unavailable","Hosted jobs are not configured.")
    if kind not in {"daily","inventory"}:raise ValueError("Unsupported hosted job")
    try:key=str(UUID(str(key)))
    except (ValueError,TypeError):raise DomainError(422,"idempotency_key_required","Supply a UUID Idempotency-Key header.") from None
    body=_payload(payload)
    if len(body.encode())>65536:raise DomainError(413,"job_too_large","The job input exceeds its byte limit.")
    job=str(uuid5(NAMESPACE_URL,"orderflow:"+principal.tenant_id+":"+key))
    digest=hashlib.sha256((kind+":"+body).encode()).hexdigest()
    with tenant_transaction(principal.tenant_id,principal.subject,principal.role) as conn:
        _member(conn,principal,operator=True)
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),{"key":principal.tenant_id+":job-budget"})
        old=conn.execute(text("SELECT kind,payload_sha256 FROM orderflow.hosted_jobs WHERE tenant_id=:t AND job_id=:j"),{"t":principal.tenant_id,"j":job}).one_or_none()
        if old:
            if old[0]!=kind or old[1]!=digest:raise DomainError(409,"job_identity_conflict","This idempotency key already identifies different job input.")
        else:
            count=conn.execute(text("SELECT count(*) FROM orderflow.hosted_jobs WHERE tenant_id=:t AND kind!='drain' AND created_at>clock_timestamp()-interval '1 day'"),{"t":principal.tenant_id}).scalar_one()
            if count>=20:raise DomainError(429,"job_budget_exceeded","The daily hosted job budget was reached.")
            conn.execute(text("INSERT INTO orderflow.hosted_jobs(tenant_id,job_id,kind,payload,payload_sha256) VALUES(:t,:j,:kind,CAST(:p AS jsonb),:h) ON CONFLICT DO NOTHING"),
                         {"t":principal.tenant_id,"j":job,"kind":kind,"p":body,"h":digest})
            actual=conn.execute(text("SELECT kind,payload_sha256 FROM orderflow.hosted_jobs WHERE tenant_id=:t AND job_id=:j"),{"t":principal.tenant_id,"j":job}).one()
            if actual[0]!=kind or actual[1]!=digest:raise DomainError(409,"job_identity_conflict","This idempotency key already identifies different job input.")
    return job


def job_status(principal,job_id):
    from .service import _member, _missing
    with tenant_transaction(principal.tenant_id,principal.subject,principal.role) as conn:
        _member(conn,principal)
        row=conn.execute(text("SELECT job_id,kind,phase,status,generation,attempts,available_at,result,last_error_code,created_at,updated_at FROM orderflow.hosted_jobs WHERE tenant_id=:t AND job_id=:j"),
                         {"t":principal.tenant_id,"j":str(job_id)}).mappings().one_or_none()
    if row is None:_missing()
    return primitive(dict(row))


def _dispatch_claims(tenant_id):
    token=str(uuid4())
    with tenant_transaction(tenant_id) as conn:
        conn.execute(text("""UPDATE orderflow.hosted_jobs SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
            generation=generation+1,lease_token=NULL,lease_until=NULL,available_at=clock_timestamp(),last_error_code='lease_expired',updated_at=clock_timestamp()
            WHERE tenant_id=:t AND status='running' AND lease_until<clock_timestamp()"""),{"t":str(tenant_id)})
        rows=conn.execute(text("""SELECT job_id,generation,available_at FROM orderflow.hosted_jobs
            WHERE tenant_id=:t AND status='pending' AND available_at<=clock_timestamp()+interval '1 hour'
            AND (dispatch_until IS NULL OR dispatch_until<clock_timestamp())
            AND (published_generation IS DISTINCT FROM generation OR published_at<clock_timestamp()-interval '5 minutes')
            ORDER BY available_at LIMIT 5 FOR UPDATE SKIP LOCKED"""),{"t":str(tenant_id)}).mappings().all()
        for row in rows:
            conn.execute(text("UPDATE orderflow.hosted_jobs SET dispatch_token=:token,dispatch_until=clock_timestamp()+interval '30 seconds' WHERE tenant_id=:t AND job_id=:j"),
                         {"t":str(tenant_id),"j":row['job_id'],"token":token})
    return [dict(row)|{"token":token} for row in rows]


async def dispatch_due(tenant_id):
    if not enabled():return 0
    rows=await asyncio.to_thread(_dispatch_claims,tenant_id)
    published=0
    for row in rows:
        accepted=False
        try:
            delay=max(0,min(3600,math.ceil((row['available_at']-datetime.now(timezone.utc)).total_seconds())))
            await send(TOPIC,{"tenant_id":str(tenant_id),"job_id":str(row['job_id']),"generation":row['generation']},
                       idempotency_key=str(row['job_id'])+":"+str(row['generation']),delay=delay,retention=86400)
            accepted=True
        except DuplicateIdempotencyKeyError:
            accepted=True
        except Exception as error:
            log.warning("Queue wakeup deferred (%s); durable outbox retained",type(error).__name__)
        with tenant_transaction(tenant_id) as conn:
            conn.execute(text("""UPDATE orderflow.hosted_jobs SET
                published_generation=CASE WHEN :ok THEN :g ELSE published_generation END,
                published_at=CASE WHEN :ok THEN clock_timestamp() ELSE published_at END,
                dispatch_token=NULL,dispatch_until=NULL
                WHERE tenant_id=:t AND job_id=:j AND generation=:g AND dispatch_token=:token"""),
                         {"t":str(tenant_id),"j":row['job_id'],"g":row['generation'],"token":row['token'],"ok":accepted})
        published+=accepted
    return published


class Wake(BaseModel):
    model_config=ConfigDict(extra="forbid")
    tenant_id: UUID
    job_id: UUID
    generation: int=Field(strict=True,ge=0)


def _claim(wake):
    tenant=str(wake.tenant_id)
    if tenant not in Settings.from_env().worker_tenants:raise ValueError("Tenant is outside the configured hosted worker allowlist")
    token=str(uuid4())
    with tenant_transaction(tenant) as conn:
        row=conn.execute(text("SELECT *,clock_timestamp() AS server_now FROM orderflow.hosted_jobs WHERE tenant_id=:t AND job_id=:j FOR UPDATE"),
                         {"t":tenant,"j":str(wake.job_id)}).mappings().one_or_none()
        if row is None or row['generation']!=wake.generation or row['status'] in {'succeeded','failed'}:return None
        if row['status']=='running' and row['lease_until']>row['server_now']:return {"retry":max(1,math.ceil((row['lease_until']-row['server_now']).total_seconds()))}
        if row['attempts']>=5:
            conn.execute(text("UPDATE orderflow.hosted_jobs SET status='failed',lease_token=NULL,lease_until=NULL,last_error_code='attempts_exhausted' WHERE tenant_id=:t AND job_id=:j"),{"t":tenant,"j":str(wake.job_id)})
            return None
        if row['available_at']>row['server_now']:return {"retry":max(1,math.ceil((row['available_at']-row['server_now']).total_seconds()))}
        conn.execute(text("UPDATE orderflow.hosted_jobs SET status='running',attempts=attempts+1,lease_token=:token,lease_until=clock_timestamp()+interval '240 seconds',updated_at=clock_timestamp() WHERE tenant_id=:t AND job_id=:j"),
                     {"t":tenant,"j":str(wake.job_id),"token":token})
        return dict(row)|{"lease_token":token,"attempts":row['attempts']+1}


def _finish(job,result,next_phase=None,available_at=None,error=None):
    from .pipeline import _assert_job_fence
    tenant=str(job['tenant_id']);job_id=str(job['job_id'])
    with tenant_transaction(tenant) as conn:
        _assert_job_fence(conn,tenant,(job_id,job['lease_token']))
        # Serialize finishing a drain with acceptance's coalesced wakeup. An
        # acceptance blocked on this job lock revives it after this commits.
        if job['kind']=='drain' and not error:
            remaining=conn.execute(text("SELECT min(available_at) FROM orderflow.inbox_events WHERE tenant_id=:t AND status IN('pending','retry_wait')"),{"t":tenant}).scalar_one()
            if remaining is not None:next_phase='start';available_at=remaining
        status='failed' if error else 'pending' if next_phase else 'succeeded'
        conn.execute(text("""UPDATE orderflow.hosted_jobs SET status=:status,phase=:phase,
            generation=generation+CASE WHEN :pending THEN 1 ELSE 0 END,
            attempts=CASE WHEN :pending THEN 0 ELSE attempts END,
            available_at=COALESCE(:at,clock_timestamp()),result=CAST(:result AS jsonb),last_error_code=:error,
            lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
            WHERE tenant_id=:t AND job_id=:j AND lease_token=:token"""),
                     {"t":tenant,"j":job_id,"token":job['lease_token'],"status":status,"phase":next_phase or 'done',"pending":bool(next_phase),"at":available_at,"result":_payload(result),"error":error})


def _execute(job):
    from . import pipeline
    tenant=str(job['tenant_id']);job_id=str(job['job_id']);lease=(job_id,str(job['lease_token']))
    payload=job['payload']
    if job['kind']=='drain':
        from .worker import drain
        result=drain([tenant],max_events=50,deadline_seconds=10)
        _finish(job,result)
        return
    if job['kind']=='daily':
        phase=job['phase']
        if phase=='start':
            with tempfile.TemporaryDirectory(prefix='orderflow-oracle-') as temporary:
                path=Path(temporary)/'oracle.json';path.write_text(_payload(payload['oracle']),encoding='utf-8')
                pipeline.stage_daily(tenant,payload['source_id'],payload['start'],payload['end'],path,run_id=job_id,lease=lease)
            _finish(job,{"run_id":job_id},next_phase='check')
        elif phase=='check':
            pipeline.check_daily(tenant,job_id,lease=lease,build_id=job['lease_token'])
            _finish(job,{"run_id":job_id},next_phase='publish')
        elif phase=='publish':
            result=pipeline.publish_daily(tenant,job_id,lease=lease)
            _finish(job,{"run_id":job_id,"has_success":result['has_success'],"stale":result['stale']})
        else:raise pipeline.PipelineError('unsupported_job_phase')
    elif job['kind']=='inventory':
        with tempfile.TemporaryDirectory(prefix='orderflow-inventory-') as temporary:
            observed=Path(temporary)/'observed.csv';reference=Path(temporary)/'reference.csv'
            observed.write_text(payload['observed_csv'],encoding='utf-8');reference.write_text(payload['reference_csv'],encoding='utf-8')
            result=pipeline.reconcile_inventory(tenant,payload['source_id'],observed,reference,run_id=job_id,lease=lease)
        _finish(job,{"run_id":result['run_id'],"comparison_status":result['status'],"quality_passed":result['status']=='pass',"difference_total":result['difference_total']})


@subscribe(topic=TOPIC,consumer_group="orderflow-jobs-v1",retry_after=30,max_concurrency=2,max_attempts=8)
async def consume(message: Message[Wake]):
    if not enabled():raise RetryAfter(60)
    wake=message.payload
    job=await asyncio.to_thread(_claim,wake)
    if job is None:return
    if 'retry' in job:raise RetryAfter(min(300,job['retry']))
    from .pipeline import PipelineError
    from .connectors import ConnectorError
    from .objects import ObjectError
    try:
        await asyncio.to_thread(_execute,job)
    except (PipelineError,ConnectorError,ObjectError,ValueError) as error:
        if getattr(error,"code",None)=='job_lease_lost':raise RetryAfter(30) from None
        await asyncio.to_thread(_finish,job,{"quality_passed":False},error=getattr(error,"code",type(error).__name__))
    except Exception:
        # Crash/connection failures retain the running lease. Redelivery may
        # reclaim it after expiry; a stale attempt cannot complete/publish.
        raise RetryAfter(60) from None
    await dispatch_due(str(wake.tenant_id))
