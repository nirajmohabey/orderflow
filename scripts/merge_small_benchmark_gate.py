"""Construct a small gate from six actual verified reports, never invented runs."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path

import benchmark

ROOT=benchmark.ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports',nargs='+',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/benchmarks/linux-corrected-10000-summary.json')
    args=parser.parse_args()
    reports=[(p,json.loads(p.read_text())) for p in args.reports]
    first=reports[0][1];issues=[]
    expected={(w,r) for w in (1,4) for r in (1,2,3)}
    keys=[(v.get('workers'),v.get('repetition')) for _,v in reports]
    if len(keys)!=len(set(keys)):issues.append('Duplicate trial identities')
    if set(keys)-expected:issues.append('Unexpected trial identity')
    if first.get('source')!=benchmark.source_manifest() or first.get('harness_sha256')!=benchmark.harness_manifest():
        issues.append('Gate source/harness is not the current frozen implementation')
    raw_root=(ROOT/'artifacts/benchmarks/raw').resolve()
    for path,report in reports:
        if report.get('schema')!='orderflow-single-small-trial-v1':issues.append(path.name+': wrong schema')
        for key in ('source','harness_sha256','wrapper_sha256','seed','mix','inputs'):
            if report.get(key)!=first.get(key):issues.append(path.name+': mismatched '+key)
        for key in ('runtime_pin','runtime_pin_kind','base_image_name','vcpus','memory_mib'):
            if report.get('declared_sandbox',{}).get(key)!=first.get('declared_sandbox',{}).get(key):issues.append(path.name+': mismatched allocation '+key)
        actual=report.get('trial',{});checks=actual.get('reconciliation',{}).get('checks',[])
        if (not report.get('passed') or not actual.get('passed') or not actual.get('owned_database_removed')
            or actual.get('source')!=first.get('source') or actual.get('harness_sha256')!=first.get('harness_sha256')
            or actual.get('http_errors')!=[] or actual.get('http_attempts')!=11000 or actual.get('distinct_accepted_ids')!=10000
            or not checks or not all(c.get('passed') for c in checks)):
            issues.append(path.name+': incomplete/failed actual trial')
        if not actual.get('raw_artifacts'):issues.append(path.name+': missing raw manifest')
        for item in actual.get('raw_artifacts',[]):
            raw=(ROOT/item['path']).resolve()
            if not raw.is_relative_to(raw_root) or not raw.is_file() or benchmark.sha(raw)!=item['sha256'] or raw.stat().st_size!=item['bytes']:
                issues.append(path.name+': missing/changed raw evidence '+Path(item['path']).name)
    if len({r.get('declared_sandbox',{}).get('name') for _,r in reports})!=len(reports):issues.append('Expected one actual trial per distinct prepared clone')
    for item in first.get('inputs',[]):
        path=(ROOT/item['path']).resolve()
        if not path.is_relative_to(raw_root) or not path.is_file() or benchmark.sha(path)!=item['sha256'] or path.stat().st_size!=item['bytes']:
            issues.append('Small input artifact missing/changed')
    complete=len(reports)==6 and set(keys)==expected
    summary={'schema':'orderflow-core-benchmark-v1','label':first.get('label'),'generated_at':datetime.now(timezone.utc).isoformat(),
             'source':first.get('source'),'harness_sha256':first.get('harness_sha256'),'seed':first.get('seed'),'mix':first.get('mix'),
             'inputs':first.get('inputs'),'environment':{'topology':first.get('topology'),'per_trial':[r.get('environment') for _,r in reports]},
             'latency_definition':'Stored committed_at is a transaction marker; measured terminal latency is first independently observed committed visibility minus ingested_at on the PostgreSQL server clock, including polling/query overhead.',
             'required_trials':[{'workers':w,'repetition':r} for w,r in sorted(expected)],
             'runs':[r['trial'] for _,r in reports if r.get('trial')],
             'complete':complete,'passed':complete and not issues,'issues':issues,
             'actual_report_sources':[{'path':str(p),'sha256':benchmark.sha(p),'sandbox':r.get('declared_sandbox'),'wrapper_sha256':r.get('wrapper_sha256')} for p,r in reports],
             'limitations':['Only actual reported trials are included; missing/failed runs never become passes.',
                            'Prepared-snapshot clones share image/configuration and the recorded source overlay but independent host/cache/I/O noise remains.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'complete':complete,'passed':summary['passed'],'actual_reports':len(reports),'issues':issues,'gate_sha256':benchmark.sha(args.output)}))
    return 0 if summary['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
