"""Funções compartilhadas: acesso ao Supabase (REST) e sessão salva do Teams."""
import base64
import gzip
import json
import os
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SESSION_ID = "principal"

TEAMS_URL = os.environ.get("TEAMS_URL", "https://teams.microsoft.com/v2/")

# Usar as mesmas opções no login e na coleta evita a Microsoft estranhar o navegador.
CONTEXT_OPTS = dict(
    locale="pt-BR",
    timezone_id="America/Sao_Paulo",
    viewport={"width": 1400, "height": 900},
    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
)


def agora():
    return datetime.now(timezone.utc).isoformat()


def rest(method, path, body=None, prefer=None):
    if not SUPABASE_URL or not SERVICE_KEY:
        raise SystemExit("Defina SUPABASE_URL e SUPABASE_SERVICE_KEY.")
    headers = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
               "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers,
                         json=body, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Supabase {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else None


def load_state():
    rows = rest("GET", f"exiros_sessao?id=eq.{SESSION_ID}&select=state")
    if not rows:
        return None
    return json.loads(gzip.decompress(base64.b64decode(rows[0]["state"])))


def save_state(state):
    blob = base64.b64encode(gzip.compress(json.dumps(state).encode())).decode()
    rest("POST", "exiros_sessao?on_conflict=id",
         {"id": SESSION_ID, "state": blob, "atualizado_em": agora()},
         prefer="resolution=merge-duplicates,return=minimal")
