"""Run a real, loopback-only paginated catalog fixture server."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',action='append',required=True)
    parser.add_argument('--source',default='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    parser.add_argument('--page-size',type=int,default=2)
    parser.add_argument('--port',type=int,default=8015)
    args=parser.parse_args()
    records=[]
    for filename in args.input:
        for line in Path(filename).read_text(encoding='utf-8').splitlines():
            if line.strip():
                record=json.loads(line)
                if record.get('source_id',args.source)==args.source:records.append(record.get('envelope',record))
    from orderflow.connectors import catalog_app
    import uvicorn
    uvicorn.run(catalog_app(records,args.page_size),host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
