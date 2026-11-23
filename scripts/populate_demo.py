"""Replay the reviewed synthetic fixture identities; preserve existing records."""
import json
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]

def main():
    load_dotenv(ROOT/'.env',override=False)
    if os.environ.get('ODF_APP_ENV')!='local' or os.environ.get('ODF_DEMO_MODE')!='true':
        raise RuntimeError('Synthetic fixture replay requires explicit local demo mode')
    from orderflow.auth import authenticate_source
    from orderflow.schemas import parse_envelope
    from orderflow.service import accept_event
    from orderflow.worker import drain
    keys=json.loads(Path(os.environ.get('ODF_SOURCE_KEYS',str(ROOT/'.runtime/source-keys.json'))).read_text(encoding='utf-8'))
    truth=json.loads((ROOT/'fixtures/expected_state.json').read_text(encoding='utf-8'))
    tenants=json.loads((ROOT/'fixtures/seed_metadata.json').read_text(encoding='utf-8'))['tenants']
    accepted=duplicates=0
    for name in truth['sequence']:
        for line in (ROOT/'fixtures'/name).read_text(encoding='utf-8').splitlines():
            row=json.loads(line)
            source=authenticate_source(keys[row['source_id']],row['source_id'])
            result=accept_event(source,parse_envelope(json.dumps(row['envelope']).encode()))
            accepted+=not result['duplicate']
            duplicates+=result['duplicate']
        drain([t['tenant_id'] for t in tenants],max_events=1000,deadline_seconds=30)
    print(json.dumps({'new_identities':accepted,'duplicate_replays':duplicates,'existing_data_reset':False,'scope':'Synthetic local fixture stream only; source credentials not printed.'}))

if __name__=='__main__': main()
