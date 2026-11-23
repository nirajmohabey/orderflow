"""Bounded regression for dnspython's published WKS parser advisory."""
import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/security/dns-bounds.json'))
    args=parser.parse_args()
    import dns.exception
    import dns.rdata
    package_version=version('dnspython')
    valid=dns.rdata.from_text('IN','WKS','10.0.0.1 udp 65535')
    rejected=False
    try:
        # Only one above the valid port range: the old parser allocates roughly
        #8KiB, so this fixture never requests an exhaustion-sized bitmap.
        dns.rdata.from_text('IN','WKS','10.0.0.1 udp 65536')
    except dns.exception.SyntaxError:
        rejected=True
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'package':'dnspython','version':package_version,
            'advisory':'GHSA-vvf4-9x63-8xvv','checks':[{'name':'highest valid WKS service parses','passed':valid is not None},
            {'name':'first out-of-range WKS service is rejected','passed':rejected}],
            'scope':'Two fixed in-memory parser cases; no DNS/network request and no exhaustion-sized allocation.'}
    report['passed']=all(row['passed'] for row in report['checks'])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'version':package_version,'passed':report['passed']}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
