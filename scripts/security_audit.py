"""Audit exact dependency versions and publishable source without printing secrets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/security'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(name):
    return re.sub(r'[-_.]+','-',name).lower()


def request_json(url,payload=None):
    request=Request(url,data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'Content-Type':'application/json','User-Agent':'OrderFlow-security-review/1.0'})
    for attempt in range(3):
        try:
            with urlopen(request,timeout=45) as response:
                raw=response.read(8*1024*1024+1)
                if len(raw)>8*1024*1024:raise RuntimeError('Advisory response exceeds its byte budget')
                return json.loads(raw)
        except Exception:
            if attempt==2:raise
            time.sleep(attempt+1)


def dependency_audit():
    generated=datetime.now(timezone.utc).isoformat()
    groups={}
    inputs={}
    def input_file(name):
        path=ROOT/name
        inputs[name]={'sha256':sha(path),'bytes':path.stat().st_size}
        return path
    app=tomllib.loads(input_file('uv.lock').read_text(encoding='utf-8'))
    groups['application-lock']=[('PyPI',canonical(p['name']),p['version']) for p in app['package'] if 'registry' in p.get('source',{})]
    input_file('requirements.lock')
    lock=json.loads(input_file('dashboard/package-lock.json').read_text(encoding='utf-8'))
    groups['dashboard-lock']=[('npm',path.split('node_modules/')[-1],item['version']) for path,item in lock['packages'].items() if path and item.get('version')]
    pin=re.compile(r'^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;\\]+)')
    for group,name in [('dbt-lock','warehouse/requirements-dbt.lock'),('airflow-constraints-inventory','orchestration/constraints-3.12.txt')]:
        groups[group]=[('PyPI',canonical(match.group(1)),match.group(2)) for line in input_file(name).read_text().splitlines() if (match:=pin.match(line))]
    input_file('orchestration/requirements-airflow.in')
    if (ROOT/'orchestration/constraints-hardened-3.12.txt').is_file():
        groups['airflow-hardened-constraints-inventory']=[('PyPI',canonical(match.group(1)),match.group(2)) for line in input_file('orchestration/constraints-hardened-3.12.txt').read_text().splitlines() if (match:=pin.match(line))]
    missing=[]
    for group,name in [('airflow-installed','airflow-installed.json'),('dbt-installed','dbt-installed.json'),('application-installed','application-installed.json')]:
        path=OUT/name
        if group=='dbt-installed' and not path.is_file() and (OUT/'dbt-linux-installed.json').is_file():
            name='dbt-linux-installed.json';path=OUT/name
        if path.is_file():
            inputs['artifacts/security/'+name]={'sha256':sha(path),'bytes':path.stat().st_size}
            groups[group]=[('PyPI',canonical(p['name']),p['version']) for p in json.loads(path.read_text())['packages'] if canonical(p['name'])!='orderflow']
        elif group=='airflow-installed':
            missing.append(group)
    packages=sorted(set(p for rows in groups.values() for p in rows))
    queries=[{'package':{'ecosystem':ecosystem,'name':name},'version':version} for ecosystem,name,version in packages]
    results=[]
    for index in range(0,len(queries),100):
        batch=request_json('https://api.osv.dev/v1/querybatch',{'queries':queries[index:index+100]})
        if len(batch.get('results',[]))!=len(queries[index:index+100]):raise RuntimeError('Missing OSV package results')
        results.extend(batch['results'])
    findings=[]
    identifiers=set()
    for package,result in zip(packages,results):
        vulns=list(result.get('vulns',[]))
        token=result.get('next_page_token')
        while token:
            page=request_json('https://api.osv.dev/v1/query',{'package':{'ecosystem':package[0],'name':package[1]},'version':package[2],'page_token':token})
            vulns.extend(page.get('vulns',[]));token=page.get('next_page_token')
        if vulns:
            ids=sorted({v['id'] for v in vulns})
            identifiers.update(ids)
            findings.append({'ecosystem':package[0],'name':package[1],'version':package[2],
                             'groups':[group for group,rows in groups.items() if package in rows],'advisory_ids':ids})
    def advisory(identifier):
        return identifier,request_json('https://api.osv.dev/v1/vulns/'+quote(identifier,safe=''))
    with ThreadPoolExecutor(max_workers=6) as pool:
        advisories=dict(pool.map(advisory,sorted(identifiers)))
    report={'generated_at':generated,'source':'https://api.osv.dev/v1/querybatch',
            'groups':{group:{'exact_versions':len(set(rows)),'packages':[{'ecosystem':p[0],'name':p[1],'version':p[2]} for p in sorted(set(rows))]} for group,rows in groups.items()},
            'unique_exact_versions':len(packages),'findings':findings,'advisories':advisories,'raw_batch_results':results}
    (OUT/'osv-audit.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    npm=shutil.which('npm.cmd') or shutil.which('npm')
    if not npm:raise RuntimeError('npm is required for the independent registry audit')
    completed=subprocess.run([npm,'audit','--json','--package-lock-only','--registry=https://registry.npmjs.org'],cwd=ROOT/'dashboard',capture_output=True,text=True,timeout=90)
    npm_report=json.loads(completed.stdout)
    (OUT/'npm-audit.json').write_text(json.dumps(npm_report,indent=2)+'\n',encoding='utf-8')
    npm_count=npm_report.get('metadata',{}).get('vulnerabilities',{}).get('total')
    if completed.returncode not in {0,1} or npm_count is None:raise RuntimeError('The registry audit did not complete')
    required_groups={'application-lock','dashboard-lock','dbt-lock','airflow-installed','dbt-installed','application-installed'}
    required_findings=[p for p in findings if required_groups.intersection(p['groups'])]
    summary={'generated_at':generated,'inputs':inputs,'unique_exact_versions':len(packages),
             'groups':{group:{'exact_versions':len(set(rows)),'packages_with_advisories':sum(group in p['groups'] for p in findings)} for group,rows in groups.items()},
             'required_graph_packages_with_advisories':len(required_findings),'unique_advisories_all_scopes':len(identifiers),
             'npm':{'exit_code':completed.returncode,'reported_vulnerabilities':npm_count,'registry':'https://registry.npmjs.org'},
             'missing_required_graphs':missing,'required_graph_passed':not required_findings and npm_count==0 and not missing,
             'limitations':['Airflow constraints inventory includes optional extras that are not necessarily installed; it is reported separately, never silently treated as the selected graph.',
                            'Exact-version advisory matching at query time is not proof of security, exploitability or deployment safety.',
                            'Installed snapshots describe their recorded interpreter/time; lock/platform differences remain explicit.',
                            'No dependency or advisory is suppressed/upgraded by this audit. Built-container OS packages, GitHub Actions runtime dependencies and future advisories are outside this package scan.']}
    (OUT/'audit-summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'dependency_groups':summary['groups'],'required_graph_passed':summary['required_graph_passed'],'missing_required_graphs':missing,'npm_vulnerabilities':npm_count}))
    return 0 if summary['required_graph_passed'] else 2 if missing and not required_findings and npm_count==0 else 1


def exposure_audit():
    paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    values={}
    sources=[]
    for name in ('.env','.env.local','.env.docker','.env.production'):
        path=ROOT/name
        if not path.is_file():continue
        sources.append(name)
        for key,value in dotenv_values(path,interpolate=False).items():
            if not isinstance(value,str):continue
            token='TOKEN' in key and not key.endswith(('_URL','_TYPE','_ENDPOINT','_SCOPE'))
            if (any(p in key for p in ('SECRET','PASSWORD','API_KEY')) or token) and len(value)>=16:
                values[name+':'+key]=value
            if key.endswith(('_DATABASE_URL','_METADATA_URL','_CONN')):
                password=unquote(urlsplit(value).password or '')
                if len(password)>=16:values[name+':'+key+' password']=password
    def private_values(value,label):
        if isinstance(value,dict):
            for key,item in value.items():private_values(item,label+':'+key)
        elif isinstance(value,list):
            for index,item in enumerate(value):private_values(item,label+':'+str(index))
        elif isinstance(value,str) and len(value)>=16:
            # For private credential registries only, not arbitrary projectJSON.
            values[label]=value
            if '://' in value:
                password=unquote(urlsplit(value).password or '')
                if len(password)>=16:values[label+' password']=password
    for name in ('.runtime/local-credentials.json','.runtime/source-keys.json','.runtime/ci-secrets.json'):
        path=ROOT/name
        if path.is_file():
            sources.append(name);private_values(json.loads(path.read_text()),name)
    patterns={'GitHub token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})'),
              'AWS access key':re.compile(r'(?:AKIA|ASIA)[A-Z0-9]{16}'),
              'private key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
              'provider key':re.compile(r'sk-(?:proj-)?[A-Za-z0-9_-]{40,}')}
    binaries={'.png','.jpg','.jpeg','.webm','.mp4','.zip','.pdf','.ico','.gz','.woff','.woff2','.ttf','.pyc'}
    findings=[];scanned=0
    for name in sorted(set(filter(None,paths))):
        path=ROOT/name
        if not path.is_file():continue
        parts=Path(name).parts
        if any(part in {'.runtime','.vercel','.git','node_modules','work'} or part.startswith('.venv') for part in parts):
            findings.append({'path':name,'kind':'private runtime directory'})
        if any(part.startswith('.env') and part!='.env.example' and not part.endswith('.example') for part in parts):
            findings.append({'path':name,'kind':'environment file'})
        if path.suffix.lower() in binaries:continue
        content=path.read_text(encoding='utf-8',errors='replace');scanned+=1
        for label,value in values.items():
            if value in content:findings.append({'path':name,'kind':'exact local credential','source_label':label})
        for kind,pattern in patterns.items():
            if pattern.search(content):findings.append({'path':name,'kind':kind})
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'text_files_scanned':scanned,'unique_local_values_checked':len(set(values.values())),
            'private_source_files':sources,'findings':findings,
            'limitations':['Current Git index and unignored files only; deleted history, external services and binary contents are excluded.',
                           'Known-prefix and exact-generated-value checks are not an exhaustive secret detector. Values are never printed.']}
    (OUT/'source-exposure.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'source_files_scanned':scanned,'unique_private_values_checked':report['unique_local_values_checked'],'findings':findings}))
    return int(bool(findings))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dependencies',action='store_true')
    parser.add_argument('--source',action='store_true')
    args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    selected=args.dependencies or args.source
    codes=[]
    if args.dependencies or not selected:codes.append(dependency_audit())
    if args.source or not selected:codes.append(exposure_audit())
    raise SystemExit(max(codes,default=0))


if __name__=='__main__':main()
