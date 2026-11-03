"""Consulta o Exiros bot no Teams para cada RC de forma interpretativa e resiliente,
gravando o resultado em public.exiros_rc.

Uso:  python exiros_sync.py                   -> RCs de TODOS os projetos (suprimentos.rc_numero)
      python exiros_sync.py --projeto 4       -> só as RCs do projeto 4 (ou PROJETO_ID=4 no ambiente)
      python exiros_sync.py --rc 33983723     -> só essa RC
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
TEXTBOX = ('[data-tid="ckeditor"], [data-tid="ckeditor-replyConversation"], div[role="textbox"][contenteditable="true"], '
           'div[contenteditable="true"][aria-label*="mensagem" i], div[contenteditable="true"][aria-label*="message" i]')
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
            f.write(page.url + "\n\n" + resumo_pagina(page) + "\n\n" + chat_text(page)[-6000:])
    except Exception:
        pass


LAUNCHER = re.compile(r"usar (o |este )?aplicativo (da )?web|use (the|this) web app|continuar (neste|no) navegador|continue on this browser", re.I)
AVISOS = re.compile(r"^(entendi|got it|pular|skip|agora n[aã]o|not now|dispensar|dismiss|talvez mais tarde|maybe later)$", re.I)


def _clicar_primeiro(page, padrao, roles=("button", "link")):
    """Clica no primeiro elemento visível cujo nome combina com o padrão."""
    for role in roles:
        try:
            loc = page.get_by_role(role, name=padrao)
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=3000)
                    return True
        except Exception:
            pass
    return False


def fechar_avisos(page):
    """Tela 'abrir no aplicativo do Teams?' e pop-ups de boas-vindas que escondem o chat."""
    a = _clicar_primeiro(page, LAUNCHER)
    b = _clicar_primeiro(page, AVISOS, roles=("button",))
    return a or b


def buscar_bot(page):
    """Último recurso: acha o bot pela busca do Teams."""
    try:
        caixa = page.get_by_role("combobox", name=re.compile(r"pesquis|search", re.I)).first
        if not caixa.count() or not caixa.is_visible():
            caixa = page.get_by_placeholder(re.compile(r"pesquis|search", re.I)).first
        caixa.click(timeout=3000)
        page.keyboard.type(BOT_NAME, delay=60)
        time.sleep(3)
        for alvo in (page.get_by_role("option", name=re.compile(BOT_NAME, re.I)),
                     page.get_by_text(re.compile(BOT_NAME, re.I))):
            if alvo.count():
                alvo.first.click(timeout=3000)
                return True
    except Exception:
        pass
    return False


def resumo_pagina(page):
    """O que o robô está vendo (vai para o log do app, para dar para entender o erro sem abrir o GitHub)."""
    partes = []
    try:
        partes.append(f"URL: {page.url[:110]}")
    except Exception:
        pass
    try:
        partes.append(f"título: {page.title()[:70]!r}")
    except Exception:
        pass
    partes.append("TEAMS_CHAT_URL definido: " + ("sim" if CHAT_URL else "não"))
    try:
        nomes = []
        for role in ("button", "treeitem", "link"):
            loc = page.get_by_role(role)
            for i in range(min(loc.count(), 25)):
                el = loc.nth(i)
                if el.is_visible():
                    t = (el.get_attribute("aria-label") or el.text_content(timeout=400) or "").strip().replace("\n", " ")[:35]
                    if t:
                        nomes.append(f"{role}:{t}")
                if len(nomes) >= 12:
                    break
        if nomes:
            partes.append("na tela: " + " | ".join(nomes))
    except Exception:
        pass
    return " · ".join(partes)[:760]


def abrir_chat(page):
    page.goto(CHAT_URL or TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
    inicio = time.time()
    clicou = bool(CHAT_URL)
    foi_aba = buscou = False
    while time.time() - inicio < 120:
        dec = time.time() - inicio
        if re.search(r"login\.(microsoftonline|live)\.com", page.url) and dec > 25:
            raise SessaoExpirada("Teams pediu login novamente")
        if page.locator(TEXTBOX).count():
            return
        fechar_avisos(page)  # "usar o aplicativo da Web", pop-ups de novidades etc.
        if not clicou:
            if not foi_aba and dec > 6:  # garante que a lista de conversas está aberta
                foi_aba = True
                _clicar_primeiro(page, re.compile(r"^(chat|bate-papo|conversas?)\b", re.I), roles=("button", "tab"))
            for alvo in (page.get_by_role("treeitem", name=re.compile(BOT_NAME, re.I)),
                         page.get_by_text(re.compile(BOT_NAME, re.I))):
                try:
                    if alvo.count():
                        alvo.first.click(timeout=3000)
                        clicou = True
                        break
                except Exception:
                    pass
            if not clicou and not buscou and dec > 45:
                buscou = True
                clicou = buscar_bot(page)
        time.sleep(2)
    raise TimeoutError("Não consegui abrir o chat do bot. " + resumo_pagina(page))


def enviar(page, texto):
    box = page.locator(TEXTBOX).last
    box.click()
    page.keyboard.type(texto, delay=35)
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
    """Lê, interpreta o estado atual do chat dinamicamente e responde com pausas de segurança."""
    
    # Acorda o bot no início
    enviar(page, "Olá")
    time.sleep(3)
    
    limite_tentativas = 25
    tentativa = 0
    inicio_geral = time.time()
    
    # Controla o fluxo interno para saber o que já foi enviado para esta RC específica
    etapa_enviada = {
        "status_doc": False,
        "tipo_rc": False,
        "numero_rc": False,
        "todos_itens": False
    }

    ultimo_texto_analisado = ""
    vezes_texto_estagnado = 0
    
    while tentativa < limite_tentativas and time.time() - inicio_geral < 150:
        tentativa += 1
        time.sleep(2.5)  # Pausa obrigatória de respiro para o chat renderizar
        
        texto_tela = chat_text(page)
        texto_norm = _norm(texto_tela)
        
        # 1. Se os itens da RC já terminaram de carregar por completo, retorna o texto
        if FIM_ITEM in texto_tela and rc in texto_tela:
            print(f"-> RC {rc}: Dados carregados com sucesso! Estabilizando leitura...")
            time.sleep(4)
            return chat_text(page)

        # Proteção contra congelamento: se o texto da tela não mudar por muito tempo, força uma interação de resgate
        if texto_tela == ultimo_texto_analisado:
            vezes_texto_estagnado += 1
            if vezes_texto_estagnado >= 5:
                print("-> Aviso: O chat parece estagnado. Enviando 'Menu' para destravar...")
                enviar(page, "Menu")
                time.sleep(3)
                vezes_texto_estagnado = 0
                continue
        else:
            vezes_texto_estagnado = 0
            ultimo_texto_analisado = texto_tela

        # 2. INTERPRETAÇÃO DINÂMICA DA TELA:
        
        # A. Se o bot está pedindo para selecionar uma opção (mensagem em inglês comum no seu log) ou menu principal
        if not etapa_enviada["status_doc"] and (any(k in texto_norm for k in ["como posso te ajudar", "como posso ajudar", "how can i help", "bem-vindo", "select one of the options"])):
            print("-> Interpretado: Menu principal / Seleção de opções detectada. Clicando em 'Status de documentos'...")
            if not clicar_se_visivel(page, r"Status de documentos"):
                enviar(page, "Status de documentos")
            etapa_enviada["status_doc"] = True
            time.sleep(3)
            continue

        # B. Se o bot pediu o tipo de documento
        if etapa_enviada["status_doc"] and not etapa_enviada["tipo_rc"] and (any(k in texto_norm for k in ["qual tipo de documento", "qual o tipo", "what type", "holding: ternium"])):
            print("-> Interpretado: Solicitação de tipo de documento detectada. Selecionando 'RC'...")
            if not clicar_se_visivel(page, r"^\s*RC\s*$"):
                enviar(page, "RC")
            etapa_enviada["tipo_rc"] = True
            time.sleep(3)
            continue

        # C. Se o bot pediu o número da RC
        if etapa_enviada["tipo_rc"] and not etapa_enviada["numero_rc"] and (any(k in texto_norm for k in ["digite o numero da sua rc", "informe o numero", "enter your", "numero da rc"])):
            print(f"-> Interpretado: Solicitação de número de RC detectada. Enviando RC: {rc}...")
            enviar(page, rc)
            etapa_enviada["numero_rc"] = True
            time.sleep(3)
            continue

        # D. Se o bot pediu o número do item após receber a RC
        if etapa_enviada["numero_rc"] and not etapa_enviada["todos_itens"] and (any(k in texto_norm for k in ["digite o numero do item", "item number", "aguarde"])):
            # Verifica se já enviou o comando "Todos" para evitar duplicidade
            if "todos" not in texto_norm[-100:]:
                print("-> Interpretado: Solicitação de item detectada. Enviando 'Todos'...")
                enviar(page, "Todos")
                etapa_enviada["todos_itens"] = True
                time.sleep(4)
                continue

        # E. Fallback geral de segurança caso o bot mande mensagem de erro ou reinicialização
        if "desculpe" in texto_norm or "nao entendi" in texto_norm:
            print("-> Interpretado: Bot indicou erro de entendimento. Enviando 'Olá' para recomeçar o fluxo...")
            enviar(page, "Olá")
            # Reseta as travas para refazer o ciclo de forma limpa
            etapa_enviada = {"status_doc": False, "tipo_rc": False, "numero_rc": False, "todos_itens": False}
            time.sleep(3)
            continue

    # Verificação final de resgate caso os dados estejam na tela mas o loop tenha encerrado
    texto_final = chat_text(page)
    if rc in texto_final and len(texto_final) > 300:
        return texto_final

    raise TimeoutError(f"A interpretação inteligente esgotou o tempo limite para a RC {rc}.")


# ---------- Supabase ----------
def listar_rcs(projeto_id=None):
    filtro = f"&projeto_id=eq.{projeto_id}" if projeto_id else ""
    rows = rest("GET", f"suprimentos?select=rc_numero&rc_numero=not.is.null{filtro}")
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
    ap.add_argument("--projeto", help="id do projeto: atualiza só as RCs dele (vazio = todos)")
    args = ap.parse_args()

    # Projeto (opcional): vem de --projeto ou da variável PROJETO_ID (entrada projeto_id do workflow)
    pid_txt = (args.projeto or os.environ.get("PROJETO_ID", "")).strip()
    projeto_id = None
    if pid_txt:
        if not re.fullmatch(r"\d+", pid_txt):
            print(f"PROJETO_ID inválido: {pid_txt!r}", file=sys.stderr)
            return 1
        projeto_id = int(pid_txt)

    rcs = [re.sub(r"\D", "", r) for r in (args.rc or [])]
    if not rcs and os.environ.get("RC_INPUT", "").strip():
        rcs = [re.sub(r"\D", "", r) for r in re.split(r"[,; ]+", os.environ["RC_INPUT"]) if r.strip()]
    rcs = [r for r in rcs if r] or listar_rcs(projeto_id)

    # projeto_id no log = esta execução cobre só esse projeto (nulo = todos); o app usa isso para mostrar o status de cada projeto
    log = {"status": "executando"}
    if projeto_id:
        log["projeto_id"] = projeto_id
    log_id = rest("POST", "exiros_sync_log", log, prefer="return=representation")[0]["id"]
    if not rcs:
        log_fim(log_id, "erro", 0, 0, "Nenhuma RC encontrada em suprimentos.rc_numero" + (f" para o projeto {projeto_id}" if projeto_id else ""))
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
            # Tenta até 2 vezes por RC: se houver falha, faz um refresh completo do chat do zero
            for tentativa_rc in range(2):
                try:
                    if tentativa_rc > 0:
                        print(f"RC {rc}: Realizando refresh completo do chat e tentando novamente...")
                        abrir_chat(page)
                        time.sleep(4)

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

            time.sleep(3)

        save_state(ctx.storage_state())
        browser.close()

    status = "ok" if not erros else ("parcial" if total else "erro")
    log_fim(log_id, status, len(rcs), total, "; ".join(erros))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
