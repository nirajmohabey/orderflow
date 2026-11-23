"""Render actual completed benchmark series; never fill in missing trials."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import html
import io
import json
import statistics
import shutil
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT if (ROOT/'src/orderflow').is_dir() else ROOT/'outputs/orderflow'
OUT=PROJECT.parent
SMALL='artifacts/benchmarks/linux-corrected-10000-summary.json'
LARGE='artifacts/benchmarks/linux-corrected-large-100000-summary.json'
if not __debug__ or sys.flags.optimize:
    raise RuntimeError('Analysis requires assertions enabled')
sys.path.insert(0,str(PROJECT/'scripts'))
import release_manifest as verification
from release_manifest import Evidence,verify_benchmarks
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--partial-audit',action='store_true',help='Show the original failed API-postflight trial explicitly; never promote it to a passing gate')
PARTIAL=parser.parse_args().partial_audit
audit_evidence=Evidence()
if not PARTIAL:
    verify_benchmarks(audit_evidence,SMALL,LARGE)

def read(name):
    return json.loads((PROJECT/name).read_text(encoding='utf-8'))

def digest(name):
    return hashlib.sha256((PROJECT/name).read_bytes()).hexdigest()

small,large=read(SMALL),read(LARGE)
PROFILE='artifacts/checks/claim-profile-comparison.json'
profile=read(PROFILE)
for item in profile['inputs']:
    assert digest(item['path'])==item['sha256']
assert small['complete'] and small['passed'] and large['complete']
if not PARTIAL:assert large['passed']
else:
    assert large['passed'] is False
    assert large['issues']==['linux-corrected-large-100000-workers1-rep1.json: failed or incomplete actual trial']
    assert small['source']==verification.core_snapshot() and small['harness_sha256']==verification.benchmark_harness()
    audit_evidence.read(SMALL);audit_evidence.read(LARGE)
    verification.bind_raw(audit_evidence,small['inputs'])
    for run in small['runs']:
        verification.verify_trial(audit_evidence,run,small['source'],small['harness_sha256'])
assert small['source']==large['source'] and large['small_gate']['sha256']==digest(SMALL)
series={10000:small['runs'],100000:[]}
for item in large['actual_reports']:
    raw=read(item['path'])
    assert digest(item['path'])==item['sha256']
    if not PARTIAL:assert raw['passed']
    else:
        audit_evidence.bind(item['path'],item)
        assert raw['source']==small['source'] and raw['harness_sha256']==small['harness_sha256']
        assert raw['wrapper_sha256']==verification.sha(PROJECT/'scripts/benchmark_trial.py')
        assert raw['gate']['sha256']==digest(SMALL) and raw['scale']==100000
        trial=raw['trial']
        assert trial['http_attempts']==110000 and trial['distinct_accepted_ids']==100000
        assert trial['http_errors']==[] and trial['owned_database_removed']
        verification.bind_raw(audit_evidence,raw['inputs'])
        verification.bind_raw(audit_evidence,trial['raw_artifacts'])
        if raw['passed']:
            verification.verify_trial(audit_evidence,trial,small['source'],small['harness_sha256'])
        else:
            assert (raw['workers'],raw['repetition'])==(1,1) and trial['passed'] is False
            failed=[c['name'] for c in trial['reconciliation']['checks'] if not c['passed']]
            assert failed==['alpha-operator exact API count and latest total','beta-operator exact API count and latest total','existing other-tenant event API returns404']
            assert trial['elapsed_seconds']>1800
            assert trial['reconciliation']['observed_counts']=={'inbox':100000,'revisions':94000,'current':23500,'receipts':100000,'quarantines':1000,'partitions':24500}
    series[100000].append({**raw['trial'],'workers':raw['workers'],'repetition':raw['repetition']})
for runs in series.values():
    assert len(runs)==6 and {(r['workers'],r['repetition']) for r in runs}=={(w,r) for w in (1,4) for r in (1,2,3)}
    assert all(not r['http_errors'] and r['owned_database_removed'] for r in runs)
    assert sum(bool(r['passed']) for r in runs)==(5 if PARTIAL and runs is series[100000] else 6)
    assert all(r['request_concurrency']==24 for r in runs)

plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'text.usetex':False,'axes.spines.top':False,'axes.spines.right':False})
colors={1:'#335c73',4:'#087f70'}
svg={}
tables={}
records={}
for scale,runs in series.items():
    fig,axes=plt.subplots(1,3,figsize=(13,4.7),layout='constrained')
    fig.set_facecolor('#f6f7f4')
    partial_scale=PARTIAL and scale==100000
    fig.suptitle(f'OrderFlow · {scale:,} events · '+('PARTIAL: 5 passed, 1 failed API postflight' if partial_scale else '6 / 6 trials passed'),fontsize=14,fontweight='bold')
    metrics=[('Terminal receipts / second',lambda r:r['terminal_receipts_per_second'],False),
             ('HTTP acceptance p95 · ms',lambda r:r['acceptance_latency_ms']['p95'],False),
             ('Observed terminal visibility p95 · seconds',lambda r:r['observed_terminal_visibility_latency_ms']['p95']/1000,True)]
    for ax,(title,getter,logarithmic) in zip(axes,metrics):
        for w,x in ((1,0),(4,1)):
            selected=sorted((r for r in runs if r['workers']==w),key=lambda r:r['repetition'])
            vals=[getter(r) for r in selected]
            for position,value,run in zip([x-.08,x,x+.08],vals,selected):
                ax.scatter(position,value,s=65,color=colors[w] if run['passed'] else '#9c3f20',marker='o' if run['passed'] else 'X',zorder=3)
            if not partial_scale and not logarithmic and title=='Terminal receipts / second':
                ax.hlines(statistics.median(vals),x-.2,x+.2,color=colors[w],linewidth=2.5)
        ax.set_xticks([0,1],['1 worker','4 workers'])
        ax.set_xlim(-.45,1.45)
        ax.set_title(title,fontsize=10,pad=12)
        ax.grid(axis='y',alpha=.2)
        ax.set_axisbelow(True)
        if logarithmic:
            ax.set_yscale('log')
            ax.set_ylabel('Logarithmic scale')
        else:ax.set_ylim(bottom=0)
    fig.supxlabel('All trial measurements · X: failed API postflight (not a passing benchmark) · Individual trial p95 · Separate microVMs' if partial_scale else 'All three trials · Throughput line: median · Individual trial p95 · Separate 2-vCPU / 4-GiB microVMs',fontsize=9)
    buffer=io.StringIO();fig.savefig(buffer,format='svg',metadata={'Date':None})
    svg[scale]=buffer.getvalue().split('<svg',1)[1]
    svg[scale]=f'<svg role="img" aria-label="{scale:,}-event workload: all three trial results per worker configuration; exact values in the following table"'+svg[scale]
    if scale==100000:fig.savefig(OUT/'OrderFlow_Benchmark.png',dpi=160)
    plt.close(fig)
    rows=[];groups=[]
    for r in sorted(runs,key=lambda r:(r['workers'],r['repetition'])):
        result='Pass' if r['passed'] else 'FAILED: API postflight'
        rows.append(f"<tr><th>{r['workers']}</th><td>{r['repetition']}</td><td>{r['elapsed_seconds']:,.2f}</td><td>{r['terminal_receipts_per_second']:,.2f}</td><td>{r['acceptance_latency_ms']['p95']:,.2f}</td><td>{r['observed_terminal_visibility_latency_ms']['p95']/1000:,.3f}</td><td>{result}</td></tr>")
    for w in (1,4):
        selected=[r for r in runs if r['workers']==w]
        rates=[r['terminal_receipts_per_second'] for r in selected]
        groups.append({'workers':w,'actual_trials':3,'successful_trials':sum(bool(r['passed']) for r in selected),'median_terminal_receipts_per_second':None if partial_scale else statistics.median(rates),'range_terminal_receipts_per_second':[min(rates),max(rates)],'trials':[{'repetition':r['repetition'],'passed':r['passed'],'terminalReceiptsPerSecond':r['terminal_receipts_per_second'],'acceptanceP95Ms':r['acceptance_latency_ms']['p95'],'observedVisibilityP95Ms':r['observed_terminal_visibility_latency_ms']['p95']} for r in sorted(selected,key=lambda r:r['repetition'])]})
    records[scale]=groups
    tables[scale]='<div class="scroll" role="region" aria-label="All benchmark trial measurements" tabindex="0"><table><caption>All six actual trials; '+('five fully passed and one failed final API assertions' if partial_scale else 'six fully passed independent reconciliation')+'</caption><thead><tr><th>Workers</th><th>Trial</th><th>Elapsed seconds</th><th>Receipts / second</th><th>HTTP p95 ms</th><th>Visibility p95 seconds</th><th>Verification</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'

panels=''.join(f'<section id="n{n}" {"hidden" if n==10000 else ""}><h2>{n:,} events per trial</h2>{svg[n]}{tables[n]}</section>' for n in (100000,10000))
large_groups={g['workers']:g for g in records[100000]}
interpretation=[]
for w in (1,4):
    g=large_groups[w]
    acceptance=[t['acceptanceP95Ms'] for t in g['trials']]
    visibility=[t['observedVisibilityP95Ms']/1000 for t in g['trials']]
    rate_description=('observed individual rates, including the failed postflight where applicable, span' if PARTIAL else f"median {g['median_terminal_receipts_per_second']:,.2f} terminal receipts/second; range")
    interpretation.append(f"<p><strong>{w}-worker configuration:</strong> {rate_description} {g['range_terminal_receipts_per_second'][0]:,.2f}–{g['range_terminal_receipts_per_second'][1]:,.2f} terminal receipts/second. Individual trial HTTP acceptance p95s span {min(acceptance):,.2f}–{max(acceptance):,.2f} ms; observed terminal-visibility p95s span {min(visibility):,.3f}–{max(visibility):,.3f} seconds.</p>")
panels+='<section class="scope"><h2>What these trials show</h2>'+''.join(interpretation)+'<p>The acceptance and completion profiles are separate measurements. Sustained submission can accumulate a worker queue even while HTTP requests complete successfully. Queue age is therefore an operational signal alongside throughput. Separate microVMs and competition for CPU prevent these observations from isolating a causal worker-count effect.</p></section>'
query_rows=[]
for scenario in profile['scenarios']:
    before,after=scenario['before'],scenario['after']
    query_rows.append(f"<tr><th>{scenario['pending_events']:,}</th><td>{scenario['dormant_partitions']:,}</td><td>{before['execution_ms']:.3f}</td><td>{after['execution_ms']:.3f}</td><td>{before['shared_hit_blocks']:,} → {after['shared_hit_blocks']:,}</td></tr>")
panels+='<section class="scope"><h2>A bounded claim-query working set</h2><p>Separate native PostgreSQL EXPLAIN ANALYZE evidence compared one claim-query sample on equal-cardinality fresh databases. An indexed candidate prefix and point partition lock replaced a full active-backlog aggregate/sort. These are query observations, not full-application speedup measurements.</p><div class="scroll" role="region" aria-label="Claim-query profile" tabindex="0"><table><caption>One recorded sample per scenario; timings include measurement noise</caption><thead><tr><th>Pending events</th><th>Dormant partitions</th><th>Before ms</th><th>After ms</th><th>Shared buffer hits</th></tr></thead><tbody>'+''.join(query_rows)+'</tbody></table></div><p>The window considers up to 128 enabled candidate events. If every candidate partition is locked, a worker backs off until the prefix makes progress. This is an explicit fairness tradeoff. Paused-source/dead-index entries and vacuum health can still increase physical reads.</p></section>'
document='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OrderFlow · measured workload analysis</title><style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#183b49;background:#f6f7f4}body{max-width:1200px;margin:0 auto;padding:36px 24px}h1{font-size:clamp(28px,5vw,48px);line-height:1.15}h2{font-size:26px}p,li{line-height:1.7;max-width:88ch}.eyebrow{color:#087f70;font-weight:700}button{font:inherit;border:1px solid #78908d;border-radius:24px;padding:12px 20px;background:white;color:#183b49;cursor:pointer;margin:6px}button[aria-pressed=true]{background:#087f70;color:white}button:focus-visible{outline:3px solid #183b49;outline-offset:3px}section{margin:32px 0}section svg{width:100%;height:auto}.scroll{overflow:auto;background:white;border:1px solid #cbd7d8;border-radius:12px}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;min-width:750px}caption{text-align:left;padding:18px;font-weight:600}th,td{text-align:left;padding:14px;border-top:1px solid #dce5e5}code{overflow-wrap:anywhere;font-size:12px}.scope{padding:24px;border:1px solid #cbd7d8;border-radius:16px;background:white}footer{margin-top:36px;font-size:13px}a{color:#076e63} @media print{button{display:none}section[hidden]{display:block!important}body{padding:0}section{break-inside:avoid}}
</style><body><main><p class="eyebrow">ENGINEERING EVIDENCE · 23 NOVEMBER 2026</p><h1>OrderFlow: throughput, queuing and correctness</h1><p>Repeated synthetic HTTP-to-worker trials, with exact reconciliation of accepted identities, immutable revisions, current records, history, receipts, checkpoints and tenant isolation. Every plotted trial completed and removed its owned database. These measurements cover the ingestion and materialization core.</p><nav aria-label="Select workload"><button aria-pressed="true" data-target="n100000">100,000 events</button><button aria-pressed="false" data-target="n10000">10,000-event gate</button></nav>'''+panels+'''<section class="scope"><h2>Read the measurements correctly</h2><ul><li>Each trial accepts 94% distinct valid revisions, 5% distinct-identity duplicate revisions, and 1% business-invalid currency events. A further 10% HTTP identity replay gives 110,000 attempts for the larger workload. Quarantine is an expected, independently checked outcome.</li><li>HTTP acceptance and terminal visibility answer different questions. The visibility measure is an upper-bound measure using the first independent observation of committed terminal state minus ingestion time on the database clock. It includes polling and query overhead. A stored transaction marker is not labeled an exact commit timestamp.</li><li>All three repetitions are retained for both configurations. Throughput is distinct terminal receipts divided by elapsed HTTP start-to-observed-drain time. The lines show throughput medians. Individual latency p95 values remain separate; they are not averaged into a pooled percentile.</li><li>Each trial ran in a separate private 2-vCPU, 4-GiB microVM from the same immutable prepared snapshot, using PostgreSQL 18.6 and Python 3.12.14. Host, cache and filesystem noise remain. The comparison does not isolate a causal worker-count speedup.</li><li>This is a bounded closed-loop synthetic HTTP load with up to 24 in-flight HTTP requests from 24 consumer tasks and real PostgreSQL/workers. It is not an open-loop arrival-rate capacity test, a hosted service SLO, dbt/Airflow throughput, or customer impact.</li><li>A practical next measurement is a controlled arrival-rate sweep on the same reserved host, recording queue age and CPU/I/O saturation. These observations do not establish unlimited scaling or a general latency guarantee.</li></ul></section></main><footer><p>Core + migrations SHA-256: <code>'''+html.escape(large['source']['sha256'])+'''</code></p><p>Small gate SHA-256: <code>'''+digest(SMALL)+'''</code><br>Large series SHA-256: <code>'''+digest(LARGE)+'''</code></p><p>Prepared snapshot: <code>snap_VroLt6lB8PEDt5AnT6r1LtpdJEf0</code>. Raw inputs, truth ledgers, HTTP attempts and terminal observations are bound in the release manifest.</p><p><a href="https://portfolio-niraj-mohabeys-projects.vercel.app/projects/orderflow">OrderFlow case study</a></p></footer><script>for(const button of document.querySelectorAll('button[data-target]'))button.addEventListener('click',()=>{for(const b of document.querySelectorAll('button[data-target]')){const selected=b===button;b.setAttribute('aria-pressed',String(selected));document.getElementById(b.dataset.target).hidden=!selected}})</script></body></html>'''
(OUT/'OrderFlow_Analysis.html').write_text(document,encoding='utf-8')
if PARTIAL:
    notice='<aside class="scope" aria-label="Partial verification"><h2>Partial 100,000-event verification: 5 passed, 1 failed</h2><p>The 10,000-event series passed all six trials. The larger series completed all six workloads, but one run failed its three final API assertions. That original report remains <strong>passed=false</strong>. Its database/observer checks passed; its final API status codes were not captured. The elapsed time exceeded the fixture token lifetime, and a separate real HTTP/PostgreSQL regression reproduced expired-token rejection and fresh-session access. The corrected future harness refreshes tokens after timing; its full workload rerun is blocked by the exhausted free Sandbox quota until December 1.</p><p>The failed run is marked X and FAILED below. Its timings are observational data, not a passing benchmark. No aggregate median is reported for the partial larger series. The release benchmark gate remains <strong>not verified</strong>.</p></aside>'
    document=document.replace('<h1>OrderFlow: throughput, queuing and correctness</h1>','<h1>OrderFlow: workload and verification audit</h1>'+notice)
    document=document.replace('Repeated synthetic HTTP-to-worker trials, with exact reconciliation of accepted identities, immutable revisions, current records, history, receipts, checkpoints and tenant isolation. Every plotted trial completed and removed its owned database. These measurements cover the ingestion and materialization core.','Twelve original synthetic HTTP-to-worker trials are retained with their actual verification outcomes. Each completed its workload and removed only its owned database. The larger series has one failed API postflight; the tables and figures keep that failure visible alongside its recorded timing.')
    document=document.replace('The lines show throughput medians.','Only the fully passing 10,000-event plot shows throughput medians. The partial larger series has no aggregate median.')
    document=document.replace('Raw inputs, truth ledgers, HTTP attempts and terminal observations are bound in the release manifest.','Raw inputs, truth ledgers, HTTP attempts and terminal observations are preserved as supplemental audit evidence. Hash binding does not promote the failed trial to a passing benchmark.')
    (OUT/'OrderFlow_Analysis.html').write_text(document,encoding='utf-8')
destination=PROJECT/'artifacts/analysis';destination.mkdir(parents=True,exist_ok=True)
shutil.copy2(OUT/'OrderFlow_Benchmark.png',destination/'benchmark.png')
shutil.copy2(OUT/'OrderFlow_Analysis.html',destination/'analysis.html')
small_reports=[]
for path in sorted((PROJECT/'artifacts/benchmarks').glob('linux-corrected-10000-workers*-rep*.json')):
    relative=path.relative_to(PROJECT).as_posix();value=read(relative)
    assert value['passed'] and value['trial'] in small['runs']
    small_reports.append(audit_evidence.bind(relative))
assert len(small_reports)==6
large_reports=[audit_evidence.bind(item['path'],item) for item in large['actual_reports']]
for path in [Path(__file__).resolve(),destination/'analysis.html',destination/'benchmark.png',PROJECT/PROFILE,*[PROJECT/item['path'] for item in profile['inputs']],*[p for p in (PROJECT/'artifacts/benchmarks/protocol').rglob('*') if p.is_file()]]:
    audit_evidence.bind(path.relative_to(PROJECT).as_posix())
summary={'schema':'orderflow-analysis-v1','generated_at':datetime.now(timezone.utc).isoformat(),'status':'partial' if PARTIAL else 'verified','passed':not PARTIAL,'benchmark_gate_status':'not_verified' if PARTIAL else 'verified','small_passed':6,'large_passed':5 if PARTIAL else 6,'large_failed':1 if PARTIAL else 0,'renderer':{'matplotlib':matplotlib.__version__,'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},'units':{'throughput':'distinct terminal receipts per second','acceptance_p95':'milliseconds','observed_visibility_p95':'milliseconds'},'metric_definitions':{'throughput':'Distinct terminal receipts divided by HTTP start to observed drain duration.','observed_visibility':'Upper-bound first observed committed terminal visibility minus ingestion time on the database clock, including polling/query overhead.','request_concurrency':'Up to 24 in-flight HTTP requests from 24 consumer tasks.'},'small_summary_sha256':digest(SMALL),'large_summary_sha256':digest(LARGE),'source_sha256':large['source']['sha256'],'small_reports':small_reports,'large_reports':large_reports,'files':list(audit_evidence.files.values()),'series':records,'limitations':['The original failed trial did not record final API status codes; fixture expiry is diagnosed from source/timing and a separate independent regression, not a newly observed status in that original run.','One bounded read-only progress probe was made late in the slowest trial; separate hosts and observer overhead remain confounders.','The future v2 full workload has not been executed. The matching retry was blocked by the free Vercel Sandbox quota.'] if PARTIAL else []}
(destination/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'completed_scales':list(records),'actual_trials':12,'outputs':['OrderFlow_Analysis.html','OrderFlow_Benchmark.png']}))
