"""Consulta o Exiros bot no Teams para cada RC e grava o resultado em public.exiros_rc.

Uso:  python exiros_sync.py                -> RCs vindas de suprimentos.rc_numero
      python exiros_sync.py --rc 33983723  -> só essa RC
Variáveis: SUPABASE_URL, SUPABASE_SERVICE_KEY, RC_INPUT (opcional), TEAMS_CHAT_URL (opcional),
           EXIROS_BOT_NAME (padrão "Exiros")
"""
import argparse
import os
import re
import sys
import time
import traceback
import unicodedata

from playwright.sync_api import sync_playwright

from exiros_common import (CONTEXT_OPTS, TEAMS_URL, agora, load_state, rest, save_state)
from exiros_parser import parse_blocks

BOT_NAME = os.environ.get("EXIROS_BOT_NAME", "Exiros")
CHAT_URL = os.environ.get("TEAMS_CHAT_URL", "").strip()
TEXTBOX = '[data-tid="ckeditor"], div[role="textbox"][contenteditable="true"]'
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "debug")
FIM_ITEM = "Solicitar ação sobre este requerimento"


class SessaoExpirada(Exception):
    pass


# ---------- Teams ----------
def chat_text(page):
    for sel in ('[data-tid="message-pane-list-viewport"]', '[data-tid="chat-pane-list"]',
                '[role="main"]', "body"):
        loc = page.locator(sel)
        if loc.count():
            try:
                return loc.first.inner_text(timeout=5000)
            except Exception:
                continue
    return ""


def foto(page, nome):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    try:
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{nome}.png"), full_page=False)
        with open(os.path.join(DEBUG_DIR, f"{nome}.txt"), "w", encoding="utf-8") as f:
            f.write(page.url + "\n\n" + chat_text(page)[-6000:])
    except Exception:
        pass


def abrir_chat(page):
    page.goto(CHAT_URL or TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
    inicio = time.time()
    clicou = bool(CHAT_URL)
    while time.time() - inicio < 120:
        if re.search(r"login\.(microsoftonline|live)\.com", page.url) and time.time() - inicio > 30:
            raise SessaoExpirada("Teams pediu login novamente")
        if page.locator(TEXTBOX).count():
            return
        if not clicou:
            for alvo in (page.get_by_role("treeitem", name=re.compile(BOT_NAME, re.I)),
                         page.get_by_text(re.compile(BOT_NAME, re.I))):
                try:
                    if alvo.count():
                        alvo.first.click(timeout=3000)
                        clicou = True
                        break
                except Exception:
                    pass
        time.sleep(2)
    raise TimeoutError("Não consegui abrir o chat do bot (veja a captura em debug/)")


def enviar(page, texto):
    box = page.locator(TEXTBOX).last
    box.click()
    page.keyboard.type(texto, delay=25)
    page.keyboard.press("Enter")


def clicar(page, padrao, timeout=40):
    btn = page.get_by_role("button", name=re.compile(padrao, re.I)).last
    btn.wait_for(state="visible", timeout=timeout * 1000)
    btn.click()


# Comparação de textos do bot: sem acento, sem diferença de maiúsculas/minúsculas e com
# variações aceitas (uma frase ou uma lista de frases alternativas).
def _norm(t):
    t = unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower())


def _frases(frase):
    return [frase] if isinstance(frase, str) else list(frase)


def _conta(page, frase):
    txt = _norm(chat_text(page))
    return sum(txt.count(_norm(f)) for f in _frases(frase))


def _cauda(page, n=500):
    return re.sub(r"\s+", " ", chat_text(page)[-n:]).strip()


def aguardar_aumento(page, frase, antes, timeout=60):
    fim = time.time() + timeout
    while time.time() < fim:
        if _conta(page, frase) > antes:
            return
        time.sleep(1)
    raise TimeoutError(f"O bot não respondeu '{_frases(frase)[0]}'. Últimas mensagens do chat: ...{_cauda(page)}")


def passo(page, frase, acao, timeout=60, alternativa=None):
    """Executa `acao` e espera o bot responder `frase`. Se falhar, tenta alternativa."""
    for tentativa in range(2):
        antes = _conta(page, frase)
        try:
            acao()
            aguardar_aumento(page, frase, antes, min(timeout, 30))
            return
        except Exception:
            if tentativa == 0:
                try:
                    enviar(page, "Olá")
                    time.sleep(3)
                except Exception:
                    pass
                if alternativa:
                    try:
                        alternativa()
                        aguardar_aumento(page, frase, antes, timeout)
                        return
                    except Exception:
                        pass
                continue
            if not alternativa:
                raise
            alternativa()
            aguardar_aumento(page, frase, antes, timeout)
            return


def consultar_rc(page, rc):
    # 1. Envia "Olá" para acordar o bot e garante que o menu principal apareceu antes de prosseguir
    enviar(page, "Olá")
    
    inicio_menu = time.time()
    menu_ok = False
    while time.time() - inicio_menu < 25:
        txt = _norm(chat_text(page))
        if "como posso te ajudar" in txt or "como posso ajudar" in txt or "how can i help" in txt:
            menu_ok = True
            break
        time.sleep(1.5)
        
    if not menu_ok:
        # Se não estabilizou no menu principal, tenta mais uma vez forçando pelo método seguro
        passo(page, ("Como posso te ajudar", "Como posso ajudar", "How can I help"), lambda: enviar(page, "Olá"))
    
    time.sleep(2) # Pausa de estabilização do render do Teams
    
    # 2. Clica em "Status de documentos" de forma segura
    passo(page, ("Qual tipo de documento", "Qual o tipo de documento", "What type"),
          lambda: clicar(page, r"Status de documentos"),
          alternativa=lambda: enviar(page, "Status de documentos"))
          
    time.sleep(2) # Pausa para o bot carregar as opções de documentos (Reserva, RC, Pedido)
          
    # 3. Clica na opção "RC"
    passo(page, ("Digite o número da sua RC", "número da sua RC", "número da RC", "Informe o número", "Enter your"),
          lambda: clicar(page, r"^\s*RC\s*$"),
          alternativa=lambda: enviar(page, "RC"))

    # Validação rigorosa garantindo que o bot realmente pediu o número da RC antes de digitar
    inicio_espera = time.time()
    rc_solicitada = False
    while time.time() - inicio_espera < 20:
        txt = _norm(chat_text(page))
        if any(k in txt for k in ["numero da sua rc", "informe o numero", "enter your", "rc"]):
            rc_solicitada = True
            break
        time.sleep(1)

    if not rc_solicitada:
        # Força o envio direto por texto caso o botão de clique estivesse indisponível na interface
        enviar(page, "RC")
        time.sleep(2)

    # 4. Envia o número da RC específica com segurança
    passo(page, ("Digite o número do item", "número do item", "item number"), lambda: enviar(page, rc))
    time.sleep(2)

    # 5. Pede "Todos" os itens do requerimento
    antes_ok = _conta(page, ("Perfeito, aguarde", "Perfeito", "Please wait"))
    antes_fim = chat_text(page).count(FIM_ITEM)
    enviar(page, "Todos")
    
    try:
        aguardar_aumento(page, ("Perfeito, aguarde", "Perfeito"), antes_ok, 30)
    except Exception:
        pass

    # Espera os itens chegarem e o número parar de crescer por ~8 s
    fim, ultimo, desde = time.time() + 180, -1, time.time()
    while time.time() < fim:
        texto = chat_text(page)
        n = texto.count(FIM_ITEM)
        if n > antes_fim:
            if n != ultimo:
                ultimo, desde = n, time.time()
            elif time.time() - desde >= 8:
                return texto
        time.sleep(1.5)
        
    texto_final = chat_text(page)
    if rc in texto_final and len(texto_final) > 200:
        return texto_final
        
    raise TimeoutError("O bot não devolveu os itens a tempo ou a sessão travou no meio.")


# ---------- Supabase ----------
def listar_rcs():
    rows = rest("GET", "suprimentos?select=rc_numero&rc_numero=not.is.null")
    rcs = {re.sub(r"\D", "", r["rc_numero"] or "") for r in rows}
    return sorted(r for r in rcs if r)


def gravar(rows):
    if rows:
        rest("POST", "exiros_rc?on_conflict=rc_item", rows,
             prefer="resolution=merge-duplicates,return=minimal")


def log_fim(log_id, status, rcs, itens, msg):
    rest("PATCH", f"exiros_sync_log?id=eq.{log_id}",
         {"finalizado_em": agora(), "status": status, "rcs": rcs, "itens": itens,
          "mensagem": (msg or "")[:1000]}, prefer="return=minimal")


# ---------- principal ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rc", action="append", help="RC específica (pode repetir)")
    args = ap.parse_args()

    rcs = [re.sub(r"\D", "", r) for r in (args.rc or [])]
    if not rcs and os.environ.get("RC_INPUT", "").strip():
        rcs = [re.sub(r"\D", "", r) for r in re.split(r"[,; ]+", os.environ["RC_INPUT"]) if r.strip()]
    rcs = [r for r in rcs if r] or listar_rcs()

    log_id = rest("POST", "exiros_sync_log", {"status": "executando"},
                  prefer="return=representation")[0]["id"]
    if not rcs:
        log_fim(log_id, "erro", 0, 0, "Nenhuma RC encontrada em suprimentos.rc_numero")
        return 1

    state = load_state()
    if not state:
        log_fim(log_id, "sessao_expirada", len(rcs), 0, "Sem sessão salva. Rode login_local.py")
        return 2

    total, erros = 0, []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(storage_state=state, **CONTEXT_OPTS)
        page = ctx.new_page()
        try:
            abrir_chat(page)
        except SessaoExpirada as e:
            foto(page, "sessao_expirada")
            log_fim(log_id, "sessao_expirada", len(rcs), 0, str(e))
            browser.close()
            return 2
        except Exception as e:
            foto(page, "abrir_chat")
            log_fim(log_id, "erro", len(rcs), 0, f"abrir chat: {e}")
            browser.close()
            return 1

        for idx, rc in enumerate(rcs):
            sucesso_rc = False
            # Tenta até 2 vezes por RC: se a primeira falhar, faz um refresh completo (do zero)
            for tentativa_rc in range(2):
                try:
                    # Se for a segunda tentativa (retry), recarrega a página para limpar o chat do zero
                    if tentativa_rc > 0:
                        print(f"RC {rc}: Tentando novamente com recarregamento (refresh) do chat...")
                        abrir_chat(page)
                        time.sleep(3)

                    texto = consultar_rc(page, rc)
                    rows = parse_blocks(texto, only_rc=rc)
                    if not rows:
                        raise ValueError("bot respondeu, mas nenhum item foi reconhecido")
                    
                    gravar(rows)
                    total += len(rows)
                    print(f"RC {rc}: {len(rows)} itens gravados")
                    sucesso_rc = True
                    break
                except Exception as e:
                    print(f"RC {rc} (Tentativa {tentativa_rc + 1}): ERRO {e}", file=sys.stderr)
                    if tentativa_rc == 1:
                        foto(page, f"rc_{rc}")
                        erros.append(f"{rc}: {e}")
                        traceback.print_exc()

            time.sleep(2)

        save_state(ctx.storage_state())  # renova a sessão para a próxima execução
        browser.close()

    status = "ok" if not erros else ("parcial" if total else "erro")
    log_fim(log_id, status, len(rcs), total, "; ".join(erros))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
