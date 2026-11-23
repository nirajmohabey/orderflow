"""One actual10k trial for parallel re-gating after a recorded core correction."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path

import benchmark
from benchmark_trial import runtime_pin_kind

ROOT=benchmark.ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,choices=(1,4),required=True)
    parser.add_argument('--repetition',type=int,choices=(1,2,3),required=True)
    parser.add_argument('--seed',type=int,default=20261123)
    parser.add_argument('--runtime-pin',required=True)
    parser.add_argument('--sandbox-name',required=True)
    parser.add_argument('--base-image-name',default='vercel/sandbox/universal')
    parser.add_argument('--label',default='linux-corrected')
    parser.add_argument('--concurrency',type=int,default=24)
    parser.add_argument('--deadline-seconds',type=int,default=600)
    args=parser.parse_args()
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',args.label) or not 1<=args.concurrency<=64 or not 60<=args.deadline_seconds<=1800:
        parser.error('Invalid label, concurrency or deadline')
    try:pin_kind=runtime_pin_kind(args.runtime_pin)
    except ValueError as error:parser.error(str(error))
    source=benchmark.source_manifest();harness=benchmark.harness_manifest()
    wrapper=benchmark.sha(Path(__file__).resolve())
    data=benchmark.dataset(10000,args.seed)
    parent=ROOT/'artifacts/benchmarks'
    raw_root=parent/'raw'/f'{args.label}-10000-seed{args.seed}-{source["sha256"][:12]}'
    raw_root.mkdir(parents=True,exist_ok=True)
    input_path=raw_root/'input.jsonl.gz';ledger_path=raw_root/'truth-ledger.jsonl.gz'
    if input_path.exists() or ledger_path.exists():raise RuntimeError('Existing small-trial inputs preserved; use a fresh owned clone/label')
    benchmark.packed_json(input_path,({'phase':n,**row} for n,rows in enumerate(data['phases']) for row in rows))
    benchmark.packed_json(ledger_path,(row for _,row in sorted(data['identities'].items())))
    destination=parent/f'{args.label}-10000-workers{args.workers}-rep{args.repetition}.json'
    if destination.exists():raise RuntimeError('Existing actual trial report preserved')
    report={'schema':'orderflow-single-small-trial-v1','generated_at':datetime.now(timezone.utc).isoformat(),'label':args.label,
            'workers':args.workers,'repetition':args.repetition,'source':source,'harness_sha256':harness,'wrapper_sha256':wrapper,
            'seed':args.seed,'mix':data['mix'],'environment':benchmark.environment_metadata(),
            'inputs':[{'path':str(p.relative_to(ROOT)).replace('\\','/'),'sha256':benchmark.sha(p),'bytes':p.stat().st_size} for p in (input_path,ledger_path)],
            'declared_sandbox':{'name':args.sandbox_name,'runtime_pin':args.runtime_pin,'runtime_pin_kind':pin_kind,'base_image_name':args.base_image_name,'vcpus':2,'memory_mib':4096},
            'topology':'One actual small trial per private prepared-snapshot clone, with the same recorded source/migration overlay. Cross-VM host/cache/I/O noise is explicit; no same-host causal speedup claim.',
            'passed':False}
    interrupted=False
    try:
        actual=benchmark.trial(data,args.workers,args.repetition,raw_root/f'workers-{args.workers}-rep-{args.repetition}',source,args.concurrency,args.deadline_seconds)
        report['trial']=actual
        report['passed']=bool(actual.get('passed') and actual.get('owned_database_removed'))
        if benchmark.sha(Path(__file__).resolve())!=wrapper or benchmark.source_manifest()!=source or benchmark.harness_manifest()!=harness:
            report['passed']=False;report['failure']='Small wrapper, source or harness changed during timing'
    except KeyboardInterrupt:
        interrupted=True;report['failure']='Interrupted; partial raw evidence retained'
    except Exception as error:report['failure_type']=type(error).__name__
    finally:destination.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'result':str(destination),'passed':report['passed'],'workers':args.workers,'repetition':args.repetition}))
    return 130 if interrupted else 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
