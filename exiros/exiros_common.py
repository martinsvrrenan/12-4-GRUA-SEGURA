"""Funções compartilhadas: leitura do .env, acesso ao Supabase (REST) e sessão salva do Teams.

No PC (modo local) as variáveis vêm do arquivo .env ao lado dos scripts.
No GitHub Actions elas vêm dos Secrets (variáveis de ambiente) e têm prioridade sobre o .env.
"""
import base64
import gzip
import json
import os
import time
from datetime import datetime, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _carregar_env():
    caminho = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8-sig") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SESSION_ID = "principal"

TEAMS_URL = os.environ.get("TEAMS_URL", "https://teams.microsoft.com/v2/")

# Mesmas opções no login e na coleta. NÃO fixamos user_agent: o Chromium/Chrome do Playwright
# já se identifica com a versão real dele (um UA falso diverge dos client hints e a
# Microsoft estranha o navegador).
CONTEXT_OPTS = dict(
    locale="pt-BR",
    timezone_id="America/Sao_Paulo",
    viewport={"width": 1400, "height": 900},
)


def agora():
    return datetime.now(timezone.utc).isoformat()


def rest(method, path, body=None, prefer=None):
    if not SUPABASE_URL or not SERVICE_KEY:
        raise SystemExit("Defina SUPABASE_URL e SUPABASE_SERVICE_KEY (arquivo .env ou variáveis de ambiente).")
    headers = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
               "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    ultimo = None
    for tentativa in range(3):  # queda rápida de internet/VPN não derruba o agente
        try:
            r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers,
                                 json=body, timeout=60)
            break
        except requests.RequestException as e:
            ultimo = e
            time.sleep(2 * (tentativa + 1))
    else:
        raise RuntimeError(f"Supabase {method} {path}: sem conexão ({ultimo})")
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
