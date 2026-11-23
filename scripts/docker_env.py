"""Create explicitly local, ignored Compose credentials without displaying them."""
from pathlib import Path
import secrets

root=Path(__file__).resolve().parents[1]
target=root/'.env.docker'
if target.exists(): raise SystemExit('Existing .env.docker preserved.')
values={name:secrets.token_urlsafe(36) for name in ('POSTGRES_PASSWORD','APP_ROLE_PASSWORD','TRANSFORM_ROLE_PASSWORD','ODF_AUTH_SECRET')}
target.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf-8')
print('Created ignored local fixture credentials. Run: docker compose --env-file .env.docker up --build -d')

