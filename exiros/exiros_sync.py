"""Consulta o Exiros bot no Teams para cada RC de forma inteligente e interpretativa,
gravando o resultado em public.exiros_rc.

Uso:  python exiros_sync.py                -> RCs vindas de suprimentos.rc_numero
      python exiros_sync.py --rc 33983723  -> só essa RC
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


def chat_text(page):
    for sel in ('[data-tid="message-pane-list-viewport"]', '[data-tid="chat-pane-list"]',
                '[role="main"]', "body"):
        loc = page.locator(sel)
        if loc.count():
            try:
                return loc.first.inner_text(timeout=4000)
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
    while time.time() - inicio < 90:
        if re.search(r"login\.(microsoftonline|live)\.com", page.url) and time.time() - inicio > 25:
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
    page.keyboard.type(texto, delay=30)
    page.keyboard.press("Enter")


def clicar_se_visivel(page, padrao):
    """Tenta clicar em um botão interativo se ele estiver visível na tela."""
    try:
        btn = page.get_by_role("button", name=re.compile(padrao, re.I)).last
        if btn.is_visible():
            btn.click()
            return True
    except Exception:
        pass
    return False


def _norm(t):
    t = unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower())


def consultar_rc_inteligente(page, rc):
    """Interpreta o texto atual da tela do bot e decide dinamicamente o que fazer em loop."""
    
    # Inicia a conversa mandando um Olá para acordar o bot
    enviar(page, "Olá")
    
    # Máximo de interações dinâmicas para evitar loop infinito caso o bot fique preso
    limite_passos = 15
    passo_atual = 0
    
    # Controladores para saber em que estágio estamos da consulta da RC
    etapa = "AGUARDANDO_MENU"
    
    # Armazena o texto anterior para detectar se o bot respondeu algo novo
    ultimo_texto_lido = ""
    
    inicio_geral = time.time()
    
    while passo_atual < limite_passos and time.time() - inicio_geral < 120:
        passo_atual += 1
        time.sleep(2)  # Pausa breve para o render do chat
        
        texto_tela = chat_text(page)
        texto_norm = _norm(texto_tela)
        
        # Se os itens já terminaram de carregar e o bloco final está presente, retorna o texto completo
        if FIM_ITEM in texto_tela and rc in texto_tela:
            # Garante que estabilizou por alguns segundos
            time.sleep(4)
            return chat_text(page)

        # MÁQUINA DE ESTADO BASEADA NA INTERPRETAÇÃO DO TEXTO DA TELA:
        
        # 1. Se o bot apresentou o menu principal (pergunta como ajudar)
        if etapa == "AGUARDANDO_MENU" and any(k in texto_norm for k in ["como posso te ajudar", "como posso ajudar", "how can i help", "bem-vindo"]):
            print("-> Interpretado: Menu principal detectado. Clicando em 'Status de documentos'...")
            if not clicar_se_visivel(page, r"Status de documentos"):
                enviar(page, "Status de documentos")
            etapa = "AGUARDANDO_TIPO_DOCUMENTO"
            continue

        # 2. Se o bot pediu o tipo de documento
        if etapa == "AGUARDANDO_TIPO_DOCUMENTO" and any(k in texto_norm for k in ["qual tipo de documento", "qual o tipo", "what type"]):
            print("-> Interpretado: Menu de documentos detectado. Selecionando 'RC'...")
            if not clicar_se_visivel(page, r"^\s*RC\s*$"):
                enviar(page, "RC")
            etapa = "AGUARDANDO_NUMERO_RC"
            continue

        # 3. Se o bot pediu o número da RC
        if etapa == "AGUARDANDO_NUMERO_RC" and any(k in texto_norm for k in ["digite o numero", "informe o numero", "enter your", "numero da sua rc"]):
            print(f"-> Interpretado: Solicitação de RC detectada. Enviando número: {rc}...")
            enviar(page, rc)
            etapa = "AGUARDANDO_OPCAO_ITENS"
            continue

        # 4. Se o bot pediu o número do item / opção de listar itens
        if etapa == "AGUARDANDO_OPCAO_ITENS" and any(k in texto_norm for k in ["numero do item", "item number", "digite o numero do item"]):
            print("-> Interpretado: Solicitação de itens detectada. Enviando 'Todos'...")
            enviar(page, "Todos")
            etapa = "COLETANDO_DADOS"
            continue

        # 5. Fallback dinâmico caso o bot mude de assunto ou mande uma mensagem genérica de erro/retorno
        if "select one of the options" in texto_norm or "desculpe" in texto_norm or "nao entendi" in texto_norm:
            print("-> Interpretado: Bot pediu redefinição ou não entendeu. Reiniciando fluxo com 'Olá'...")
            enviar(page, "Olá")
            etapa = "AGUARDANDO_MENU"
            continue
            
        # Se estivermos na etapa de coleta de dados, apenas aguardamos o volume de itens crescer
        if etapa == "COLETANDO_DADOS":
            time.sleep(2)
            # Se já passou bastante tempo e os itens apareceram na tela, podemos retornar
            if rc in texto_tela and texto_tela.count(FIM_ITEM) > 0:
                time.sleep(3)
                return texto_tela

    # Verificação de resgate final caso o chat contenha os dados brutos mas o loop tenha esgotado
    texto_final = chat_text(page)
    if rc in texto_final and len(texto_final) > 300:
        return texto_final

    raise TimeoutError("A interpretação dinâmica esgotou o tempo limite sem concluir o fluxo do bot.")


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
            for tentativa_rc in range(2):
                try:
                    if tentativa_rc > 0:
                        print(f"RC {rc}: Realizando refresh completo do chat e tentando novamente...")
                        abrir_chat(page)
                        time.sleep(3)

                    texto = consultar_rc_inteligente(page, rc)
                    rows = parse_blocks(texto, only_rc=rc)
                    if not rows:
                        raise ValueError("bot respondeu, mas nenhum item foi reconhecido")
                    
                    gravar(rows)
                    total += len(rows)
                    print(f"RC {rc}: {len(rows)} itens gravados com sucesso!")
                    sucesso_rc = True
                    break
                except Exception as e:
                    print(f"RC {rc} (Tentativa {tentativa_rc + 1}): ERRO -> {e}", file=sys.stderr)
                    if tentativa_rc == 1:
                        foto(page, f"rc_{rc}")
                        erros.append(f"{rc}: {e}")
                        traceback.print_exc()

            time.sleep(2)

        save_state(ctx.storage_state())
        browser.close()

    status = "ok" if not erros else ("parcial" if total else "erro")
    log_fim(log_id, status, len(rcs), total, "; ".join(erros))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
