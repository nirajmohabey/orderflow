"""Verify and summarize actual large-trial artifacts; never synthesize runs."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics

import benchmark

ROOT=benchmark.ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports',nargs='+',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/benchmarks/linux-parallel-100000-summary.json')
    args=parser.parse_args()
    loaded=[];issues=[]
    for path in args.reports:
        value=json.loads(path.read_text(encoding='utf-8'))
        loaded.append((path,value))
    reference=loaded[0][1]
    required={(workers,repetition) for workers in (1,4) for repetition in (1,2,3)}
    identities=[(v.get('workers'),v.get('repetition')) for _,v in loaded]
    if len(identities)!=len(set(identities)):issues.append('Duplicate trial identities; no fastest-run selection is permitted')
    observed=set(identities)
    if observed-required:issues.append('Unexpected trial identity')
    for path,value in loaded:
        if value.get('schema')!='orderflow-single-large-trial-v1' or value.get('scale')!=100000:
            issues.append(path.name+': wrong report schema/scale')
        for key in ('source','harness_sha256','wrapper_sha256','seed','mix','inputs'):
            if value.get(key)!=reference.get(key):issues.append(path.name+': inconsistent '+key)
        if value.get('gate',{}).get('sha256')!=reference.get('gate',{}).get('sha256'):
            issues.append(path.name+': a different small gate was used')
        for key in ('runtime_pin','runtime_pin_kind','base_image_name','vcpus','memory_mib'):
            if value.get('declared_sandbox',{}).get(key)!=reference.get('declared_sandbox',{}).get(key):issues.append(path.name+': inconsistent declared '+key)
        trial=value.get('trial',{})
        checks=trial.get('reconciliation',{}).get('checks',[])
        if (not value.get('passed') or not trial.get('passed') or not trial.get('owned_database_removed')
            or trial.get('http_errors')!=[] or trial.get('distinct_accepted_ids')!=100000
            or trial.get('http_attempts')!=110000 or not checks or not all(c.get('passed') for c in checks)):
            issues.append(path.name+': failed or incomplete actual trial')
        raw=trial.get('raw_artifacts',[])
        if not raw:issues.append(path.name+': no raw evidence manifest')
        for item in raw:
            raw_path=(ROOT/item['path']).resolve()
            if (not raw_path.is_relative_to((ROOT/'artifacts/benchmarks/raw').resolve()) or not raw_path.is_file()
                or benchmark.sha(raw_path)!=item['sha256'] or raw_path.stat().st_size!=item['bytes']):
                issues.append(path.name+': missing/changed raw evidence '+Path(item['path']).name)
    sandboxes=[v.get('declared_sandbox',{}).get('name') for _,v in loaded]
    if len(set(sandboxes))!=len(sandboxes):issues.append('The declared one-trial-per-VM topology has duplicate sandbox names')
    for item in reference.get('inputs',[]):
        path=(ROOT/item['path']).resolve()
        if not path.is_file() or not path.is_relative_to((ROOT/'artifacts/benchmarks/raw').resolve()) or benchmark.sha(path)!=item['sha256']:
            issues.append('Missing/changed frozen large input '+Path(item['path']).name)
    groups={}
    for workers in (1,4):
        trials=[v['trial'] for _,v in loaded if v.get('workers')==workers and v.get('trial')]
        rates=[v['terminal_receipts_per_second'] for v in trials if v.get('passed') and 'terminal_receipts_per_second' in v]
        groups[str(workers)]={'actual_trials':len(trials),'successful_trials':len(rates),'terminal_receipts_per_second':rates,
                              'median_terminal_receipts_per_second':statistics.median(rates) if rates else None,
                              'range_terminal_receipts_per_second':[min(rates),max(rates)] if rates else None,
                              'per_trial_p95_acceptance_ms':[v.get('acceptance_latency_ms',{}).get('p95') for v in trials],
                              'per_trial_p95_observed_visibility_ms':[v.get('observed_terminal_visibility_latency_ms',{}).get('p95') for v in trials]}
    summary={'schema':'orderflow-parallel-large-series-v1','generated_at':datetime.now(timezone.utc).isoformat(),
             'complete':observed==required and len(loaded)==6,'passed':observed==required and len(loaded)==6 and not issues,
             'issues':issues,'missing_trials':[{'workers':w,'repetition':r} for w,r in sorted(required-observed)],
             'source':reference.get('source'),'harness_sha256':reference.get('harness_sha256'),'wrapper_sha256':reference.get('wrapper_sha256'),
             'small_gate':reference.get('gate'),'seed':reference.get('seed'),'inputs':reference.get('inputs'),'mix':reference.get('mix'),
             'topology':reference.get('topology'),'declared_allocation':{k:reference.get('declared_sandbox',{}).get(k) for k in ('runtime_pin','runtime_pin_kind','base_image_name','vcpus','memory_mib')},
             'groups':groups,'actual_reports':[{'path':path.as_posix(),'sha256':benchmark.sha(path),'workers':v.get('workers'),'repetition':v.get('repetition'),'passed':v.get('passed'),'environment':v.get('environment'),'postgresql':v.get('trial',{}).get('postgresql')} for path,v in loaded],
             'limitations':['Separate microVMs introduce host/cache/filesystem/I/O noise despite identical declared allocations and image/core/input hashes.',
                            'Per-trial p95 values are retained individually; they are not averaged or presented as a pooled percentile.',
                            'Observed throughput is local synthetic HTTP/core-worker behavior, not a hosted service SLO or a causal speedup proof.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'complete':summary['complete'],'passed':summary['passed'],'actual_reports':len(loaded),'issues':issues,'output':str(args.output)}))
    return 0 if summary['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
