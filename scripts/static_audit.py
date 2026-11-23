"""Retain full static findings and fail on unsafe or newly unreviewed results."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/security'


def signature(finding):
    path=finding['filename'].replace('\\','/').removeprefix('./')
    for prefix in ('outputs/orderflow/',str(ROOT).replace('\\','/')+'/'):
        path=path.removeprefix(prefix)
    return {'test_id':finding['test_id'],'path':path,'line':finding['line_number'],
            'snippet_sha256':hashlib.sha256(finding['code'].encode()).hexdigest()}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    done=subprocess.run([sys.executable,'-m','bandit','-r','src','scripts','orchestration/dags','-f','json','-o',str(OUT/'bandit.json')],cwd=ROOT,capture_output=True,text=True,timeout=120)
    bandit=json.loads((OUT/'bandit.json').read_text())
    lint=subprocess.run([sys.executable,'-m','ruff','check','src','scripts','tests','orchestration/dags','--select','F821,F822,F823','--output-format','json','--output-file',str(OUT/'ruff-undefined-names.json')],cwd=ROOT,capture_output=True,text=True,timeout=120)
    lint_findings=json.loads((OUT/'ruff-undefined-names.json').read_text())
    review_path=OUT/'static-triage.json'
    reviewed=json.loads(review_path.read_text()) if review_path.is_file() else {'medium_findings':[]}
    known={json.dumps(item['signature'],sort_keys=True) for item in reviewed['medium_findings']}
    unreviewed=[]
    for finding in bandit['results']:
        if finding['issue_severity']!='MEDIUM':continue
        item=signature(finding)
        reviewed_hash=reviewed.get('reviewed_medium_source_sha256',{}).get(item['path'])
        actual_hash=hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest()
        if json.dumps(item,sort_keys=True) not in known or reviewed_hash!=actual_hash:
            unreviewed.append(item)
    totals=bandit['metrics']['_totals']
    passed=done.returncode in {0,1} and not bandit['errors'] and totals['SEVERITY.HIGH']==0 and not unreviewed and lint.returncode==0 and not lint_findings
    report={'generated_at':bandit['generated_at'],'bandit_severities':{key:value for key,value in totals.items() if key.startswith('SEVERITY.')},
            'bandit_findings_suppressed':0,'unreviewed_medium_findings':unreviewed,'ruff_undefined_names':len(lint_findings),'passed':passed,
            'policy':'Keep every Bandit severity in the raw report; fail on high findings, scan errors, undefined names or changed/unreviewed medium call-site signatures. Reviewed dispositions are bounded assessments, not a security guarantee.'}
    (OUT/'static-audit-summary.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
