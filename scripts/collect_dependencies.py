"""Record installed package names/versions only, never environment credentials."""
import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import platform
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    packages=sorted([{'name':dist.metadata['Name'],'version':dist.version} for dist in metadata.distributions() if dist.metadata.get('Name')],key=lambda p:(p['name'].lower(),p['version']))
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'python':sys.version,'platform':platform.platform(),'packages':packages,
            'scope':'Installed distributions visible to this interpreter; package names/versions only. This is not an advisory audit.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(args.output),'packages':len(packages)}))


if __name__=='__main__':main()
