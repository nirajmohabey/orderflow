"""One large trial after a verified six-run small gate; no fabricated merging."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile

import benchmark

ROOT=benchmark.ROOT


def runtime_pin_kind(value):
    if re.fullmatch(r'snapshot:snap_[A-Za-z0-9_-]{8,128}',value):
        return 'provider_immutable_snapshot'
    if re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}',value):
        return 'image_sha256_digest'
    raise ValueError('Runtime must use a full image digest or a provider immutable snapshot ID')


def validate_gate(path, expected_sha, seed):
    if benchmark.sha(path)!=expected_sha:
        raise RuntimeError('The supplied gate bytes do not match the reviewed SHA256')
    gate=json.loads(path.read_text(encoding='utf-8'))
    if gate.get('schema')!='orderflow-core-benchmark-v1' or not gate.get('complete') or not gate.get('passed'):
        raise RuntimeError('The small-workload gate is not complete and passing')
    if gate.get('source')!=benchmark.source_manifest() or gate.get('harness_sha256')!=benchmark.harness_manifest() or gate.get('seed')!=seed:
        raise RuntimeError('Gate core, harness or seed differs from this trial')
    required={(workers,repetition) for workers in (1,4) for repetition in (1,2,3)}
    runs=gate.get('runs',[])
    if len(runs)!=6 or {(r.get('workers'),r.get('repetition')) for r in runs}!=required:
        raise RuntimeError('Gate must contain six actual one/four-worker repetitions')
    for run in runs:
        checks=run.get('reconciliation',{}).get('checks',[])
        if (not run.get('passed') or not run.get('owned_database_removed') or run.get('source')!=gate['source']
            or run.get('harness_sha256')!=gate['harness_sha256'] or run.get('http_errors')!=[]
            or run.get('distinct_accepted_ids')!=10000 or run.get('http_attempts')!=11000
            or not run.get('reconciliation',{}).get('passed') or not checks or not all(c.get('passed') for c in checks)):
            raise RuntimeError('A small-workload trial is failed, incomplete or inconsistent')
    supplied=gate.get('inputs',[])
    if len(supplied)!=2:
        raise RuntimeError('The gate input manifest is incomplete')
    raw_root=(ROOT/'artifacts/benchmarks/raw').resolve()
    expected_data=benchmark.dataset(10000,seed)
    with tempfile.TemporaryDirectory(prefix='orderflow-gate-inputs-') as folder:
        folder=Path(folder)
        benchmark.packed_json(folder/'input.jsonl.gz',({'phase':n,**row} for n,rows in enumerate(expected_data['phases']) for row in rows))
        benchmark.packed_json(folder/'truth-ledger.jsonl.gz',(row for _,row in sorted(expected_data['identities'].items())))
        for item in supplied:
            path=(ROOT/item['path']).resolve()
            if not path.is_relative_to(raw_root) or path.name not in {'input.jsonl.gz','truth-ledger.jsonl.gz'}:
                raise RuntimeError('Gate input path is outside the expected synthetic raw-artifact directory')
            if not path.is_file() or benchmark.sha(path)!=item['sha256'] or path.stat().st_size!=item['bytes']:
                raise RuntimeError('Gate input bytes are missing or changed')
            if benchmark.sha(folder/path.name)!=item['sha256']:
                raise RuntimeError('Gate inputs do not match the frozen independent generator and seed')
    return gate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale',type=int,choices=(100000,),default=100000)
    parser.add_argument('--workers',type=int,choices=(1,4),required=True)
    parser.add_argument('--repetition',type=int,choices=(1,2,3),required=True)
    parser.add_argument('--seed',type=int,default=20261123)
    parser.add_argument('--gate',type=Path,required=True)
    parser.add_argument('--gate-sha256',required=True)
    parser.add_argument('--runtime-pin','--runtime-image',dest='runtime_pin',required=True)
    parser.add_argument('--base-image-name',default='vercel/sandbox/universal')
    parser.add_argument('--sandbox-name',required=True)
    parser.add_argument('--label',default='linux-parallel')
    parser.add_argument('--concurrency',type=int,default=24)
    parser.add_argument('--deadline-seconds',type=int,default=2100)
    parser.add_argument('--validate-only',action='store_true')
    args=parser.parse_args()
    if (not re.fullmatch(r'[0-9a-f]{64}',args.gate_sha256) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',args.label)
        or not 1<=args.concurrency<=64 or not 60<=args.deadline_seconds<=3600):
        parser.error('Require a SHA256 gate, pinned image digest, safe label, concurrency1–64 and deadline60–3600')
    try:
        pin_kind=runtime_pin_kind(args.runtime_pin)
    except ValueError as error:
        parser.error(str(error))
    gate=validate_gate(args.gate,args.gate_sha256,args.seed)
    manifest=benchmark.source_manifest()
    harness=benchmark.harness_manifest()
    wrapper_sha=benchmark.sha(Path(__file__).resolve())
    report={'schema':'orderflow-single-large-trial-v1','generated_at':datetime.now(timezone.utc).isoformat(),
            'workers':args.workers,'repetition':args.repetition,'scale':args.scale,'seed':args.seed,
            'gate':{'sha256':args.gate_sha256,'small_trials':6,'label':gate['label'],'inputs':gate['inputs']},
            'source':manifest,'harness_sha256':harness,'wrapper_sha256':wrapper_sha,
            'environment':benchmark.environment_metadata(),'declared_sandbox':{'name':args.sandbox_name,'runtime_pin':args.runtime_pin,'runtime_pin_kind':pin_kind,'base_image_name':args.base_image_name,'vcpus':2,'memory_mib':4096},
            'topology':'One trial per separately provisioned private Linux2-vCPU/4-GiB microVM, using the same pinned image digest or provider immutable prepared snapshot, core/locks/input generator. A snapshot ID is recorded as a provider snapshot, never represented as a fabricated base-image SHA. Cross-VM host, filesystem/cache and I/O variation remains a confounder; this is not a controlled same-host speedup experiment.',
            'latency_definition':gate['latency_definition'],'passed':False}
    if args.validate_only:
        print(json.dumps({'gate_valid':True,'source_sha256':manifest['sha256'],'harness_sha256':harness,'wrapper_sha256':wrapper_sha,'database_created':False}))
        return 0
    data=benchmark.dataset(args.scale,args.seed)
    parent=ROOT/'artifacts/benchmarks'
    raw_root=parent/'raw'/f'{args.label}-{args.scale}-seed{args.seed}-{manifest["sha256"][:12]}'
    raw_root.mkdir(parents=True,exist_ok=True)
    for name,rows in [('input.jsonl.gz',({'phase':n,**row} for n,phase in enumerate(data['phases']) for row in phase)),
                      ('truth-ledger.jsonl.gz',(row for _,row in sorted(data['identities'].items())))]:
        path=raw_root/name
        if path.exists():raise RuntimeError('Existing large-trial inputs preserved; choose a fresh owned trial directory')
        benchmark.packed_json(path,rows)
    report['inputs']=[{'path':str(p.relative_to(ROOT)).replace('\\','/'),'sha256':benchmark.sha(p),'bytes':p.stat().st_size} for p in (raw_root/'input.jsonl.gz',raw_root/'truth-ledger.jsonl.gz')]
    report['mix']=data['mix']
    destination=parent/f'{args.label}-{args.scale}-workers{args.workers}-rep{args.repetition}.json'
    if destination.exists():raise RuntimeError('Existing trial result preserved; never overwrite a repetition')
    folder=raw_root/f'workers-{args.workers}-rep-{args.repetition}'
    interrupted=False
    try:
        result=benchmark.trial(data,args.workers,args.repetition,folder,manifest,args.concurrency,args.deadline_seconds)
        report['trial']=result
        report['passed']=bool(result.get('passed') and result.get('owned_database_removed'))
        if (benchmark.sha(Path(__file__).resolve())!=wrapper_sha or benchmark.sha(args.gate)!=args.gate_sha256
            or benchmark.harness_manifest()!=harness or benchmark.source_manifest()!=manifest):
            report['passed']=False
            report['failure']='Wrapper, gate, harness or core changed during the trial'
    except KeyboardInterrupt:
        report['failure']='Interrupted; actual partial raw trial evidence retained'
        interrupted=True
    except Exception as error:
        report['failure_type']=type(error).__name__
    finally:
        destination.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'result':str(destination),'passed':report['passed'],'workers':args.workers,'repetition':args.repetition}))
    return 130 if interrupted else 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
