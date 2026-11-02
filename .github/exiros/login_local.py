"""Rode UMA vez no computador (precisa de tela) para fazer o login com MFA.
Salva a sessão do Teams no Supabase; o GitHub Actions passa a usá-la.

  pip install -r requirements.txt && playwright install chromium
  export SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...     (Windows: set VAR=valor)
  python login_local.py
"""
from playwright.sync_api import sync_playwright

from exiros_common import CONTEXT_OPTS, TEAMS_URL, save_state

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(**CONTEXT_OPTS)
    page = ctx.new_page()
    page.goto(TEAMS_URL)
    print("\n1) Faça o login completo no Teams (e-mail, senha, MFA).")
    print("2) Espere o Teams carregar e abra o chat do Exiros bot.")
    input("3) Com o chat do bot aberto, volte aqui e aperte ENTER... ")
    save_state(ctx.storage_state())
    print("Sessão salva no Supabase. Pode fechar.")
    browser.close()
