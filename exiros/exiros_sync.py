"""Consulta o Exiros bot no Teams para cada RC de forma interpretativa e resiliente,
gravando o resultado em public.exiros_rc.

Dois modos de execução:
  remoto (GitHub Actions): Chromium headless + sessão salva no Supabase (exiros_sessao)
  local  (seu PC)        : Chrome com perfil próprio e persistente (você já fica logado no Teams)

Uso:  python exiros_sync.py                   -> RCs de TODOS os projetos (suprimentos.rc_numero)
      python exiros_sync.py --projeto 4       -> só as RCs do projeto 4 (ou PROJETO_ID=4 no ambiente)
      python exiros_sync.py --rc 33983723     -> só essa RC
      python exiros_sync.py --local           -> usa o Chrome local (em vez do modo GitHub)
"""
import argparse
import os
import re
import sys
import time
import traceback
import unicodedata

from playwright.sync_api import sync_playwright

from exiros_common import (BASE_DIR, CONTEXT_OPTS, TEAMS_URL, agora, load_state, rest, save_state)
from exiros_parser import parse_blocks

BOT_NAME = os.environ.get("EXIROS_BOT_NAME", "Exiros")
CHAT_URL = os.environ.get("TEAMS_CHAT_URL", "").strip()
TEXTBOX = ('[data-tid="ckeditor"], [data-tid="ckeditor-replyConversation"], div[role="textbox"][contenteditable="true"], '
           'div[contenteditable="true"][aria-label*="mensagem" i], div[contenteditable="true"][aria-label*="message" i]')
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
PERFIL_DIR = os.environ.get("EXIROS_PERFIL_DIR", os.path.join(BASE_DIR, "perfil_chrome"))
VISIVEL = os.environ.get("EXIROS_VISIVEL", "1") == "1"  # modo local: 1 = janela do Chrome aparece (minimizada)
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


def deslogado(page):
    """True se o Teams abriu mas SEM usuário autenticado (a sessão salva não valeu).
    Sintomas vistos nos logs: título 'Chat | Unknown | Microsoft Teams' e botão 'Sign in'."""
    try:
        if re.search(r"login\.(microsoftonline|live)\.com", page.url):
            return True
        if re.search(r"\|\s*Unknown\s*\|", page.title() or ""):
            return True
        campo = page.locator('input[name="loginfmt"], input[type="email"]')
        if campo.count() and campo.first.is_visible():
            return True
        btn = page.get_by_role("button", name=re.compile(r"^(sign in|entrar)$", re.I))
        if btn.count() and btn.first.is_visible():
            return True
    except Exception:
        pass
    return False


def abrir_chat(page):
    page.goto(CHAT_URL or TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
    inicio = time.time()
    clicou = False
    foi_aba = buscou = False
    # Com TEAMS_CHAT_URL damos 30 s para o link abrir o chat; se não abrir, procuramos o bot pelo nome.
    espera_url = 30 if CHAT_URL else 0
    while time.time() - inicio < 120:
        dec = time.time() - inicio
        if page.locator(TEXTBOX).count():
            return
        if dec > 25 and deslogado(page):
            raise SessaoExpirada("Teams abriu sem usuário logado (sessão salva inválida). Rode login_local.py de novo. " + resumo_pagina(page))
        fechar_avisos(page)  # "usar o aplicativo da Web", pop-ups de novidades etc.
        if not clicou and dec > espera_url:
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


def _rc_pronta(texto, rc):
    """A resposta do bot para ESTA RC já está na tela? ('RC: <número>-<item>' só aparece
    nas respostas do bot; a mensagem que nós enviamos é só o número, sem 'RC:')."""
    n = rc.lstrip("0") or "0"
    return FIM_ITEM in texto and re.search(rf"RC:\s*0*{re.escape(n)}-\d+", texto) is not None


def _aguardar_estavel(page, maximo=40):
    """Espera o bot terminar de mandar todos os itens (a contagem de blocos para de crescer)."""
    ultimo, iguais, t0 = -1, 0, time.time()
    while time.time() - t0 < maximo:
        n = chat_text(page).count(FIM_ITEM)
        if n == ultimo:
            iguais += 1
        else:
            ultimo, iguais = n, 0
        if iguais >= 2:
            break
        time.sleep(3)
    return chat_text(page)


def _fluxo_novo():
    # base_fim = quantos blocos "Solicitar ação..." já existiam no chat ANTES de pedirmos "Todos"
    return {"status_doc": False, "tipo_rc": False, "numero_rc": False, "todos_itens": False, "base_fim": 0}


def consultar_rc_inteligente(page, rc):
    """Conduz o bot (Olá -> Status de documentos -> RC -> número -> Todos) olhando só o FIM do chat,
    para o histórico de RCs anteriores não confundir a leitura."""
    enviar(page, "Olá")
    ultima_acao = time.time()
    etapa = _fluxo_novo()
    ultimo_texto, parado, resets = "", 0, 0
    inicio = time.time()

    while time.time() - inicio < 180:
        time.sleep(2.5)
        texto = chat_text(page)

        # 1. Resposta desta RC já apareceu por completo?
        #    Só vale depois de pedirmos "Todos" E se surgiram blocos novos: o histórico do chat
        #    guarda respostas antigas da mesma RC e elas não podem passar por dado novo.
        if etapa["todos_itens"] and _rc_pronta(texto, rc) and texto.count(FIM_ITEM) > etapa["base_fim"]:
            print(f"-> RC {rc}: dados carregados, estabilizando leitura...")
            return _aguardar_estavel(page)

        cauda = _norm(texto[-1200:])  # só o que está no fim do chat

        # 2. Chat parado: recomeça o fluxo (no máximo 2 vezes)
        if texto == ultimo_texto:
            parado += 1
        else:
            parado, ultimo_texto = 0, texto
        if parado >= (16 if etapa["todos_itens"] else 8):
            if resets >= 2:
                break
            resets += 1
            print("-> Chat parado; recomeçando o fluxo com 'Olá'...")
            enviar(page, "Olá")
            etapa, parado, ultima_acao = _fluxo_novo(), 0, time.time()
            time.sleep(3)
            continue

        # 3. Interpretação do estado atual
        if not etapa["status_doc"] and any(k in cauda for k in (
                "como posso te ajudar", "como posso ajudar", "how can i help", "bem-vindo", "select one of the options")):
            print("-> Menu principal: 'Status de documentos'")
            if not clicar_se_visivel(page, r"Status de documentos"):
                enviar(page, "Status de documentos")
            etapa["status_doc"], ultima_acao = True, time.time()
            time.sleep(3)
            continue

        if etapa["status_doc"] and not etapa["tipo_rc"] and any(k in cauda for k in (
                "qual tipo de documento", "qual o tipo", "what type", "holding: ternium")):
            print("-> Tipo de documento: 'RC'")
            if not clicar_se_visivel(page, r"^\s*RC\s*$"):
                enviar(page, "RC")
            etapa["tipo_rc"], ultima_acao = True, time.time()
            time.sleep(3)
            continue

        if etapa["tipo_rc"] and not etapa["numero_rc"] and any(k in cauda for k in (
                "digite o numero da sua rc", "informe o numero", "enter your", "numero da rc")):
            print(f"-> Enviando RC {rc}")
            enviar(page, rc)
            etapa["numero_rc"], ultima_acao = True, time.time()
            time.sleep(3)
            continue

        if etapa["numero_rc"] and not etapa["todos_itens"] and any(k in cauda for k in (
                "digite o numero do item", "item number", "aguarde")):
            print("-> Item: 'Todos'")
            etapa["base_fim"] = chat_text(page).count(FIM_ITEM)
            enviar(page, "Todos")
            etapa["todos_itens"], ultima_acao = True, time.time()
            time.sleep(4)
            continue

        if time.time() - ultima_acao > 10 and ("desculpe" in cauda or "nao entendi" in cauda):
            if resets >= 3:
                break
            resets += 1
            print("-> Bot não entendeu; recomeçando com 'Olá'...")
            enviar(page, "Olá")
            etapa, ultima_acao = _fluxo_novo(), time.time()
            time.sleep(3)

    texto_final = chat_text(page)
    if etapa["todos_itens"] and _rc_pronta(texto_final, rc) and texto_final.count(FIM_ITEM) > etapa["base_fim"]:
        return texto_final
    raise TimeoutError(f"Sem resposta do bot para a RC {rc}. Etapas: {etapa}. Fim do chat: {_norm(texto_final[-200:])!r}")


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


# ---------- execução ----------
def abrir_contexto(p, local, state):
    """Devolve (browser, contexto). No modo local não há 'browser': o contexto persistente é o próprio Chrome."""
    args = ["--disable-blink-features=AutomationControlled"]
    if local:
        os.makedirs(PERFIL_DIR, exist_ok=True)
        if VISIVEL:
            args.append("--start-minimized")
        # Perfil DEDICADO (não o seu Chrome do dia a dia: o Chrome recusa automação no perfil padrão).
        # O login do Teams fica gravado nessa pasta; não precisa de state.json.
        ctx = p.chromium.launch_persistent_context(
            PERFIL_DIR, channel="chrome", headless=not VISIVEL, args=args, **CONTEXT_OPTS)
        return None, ctx
    try:
        # channel="chromium" = "novo headless": navegador completo, sem a marca 'HeadlessChrome'
        browser = p.chromium.launch(channel="chromium", headless=True, args=args)
    except Exception:
        browser = p.chromium.launch(headless=True, args=args)
    return browser, browser.new_context(storage_state=state, **CONTEXT_OPTS)


def _consultar_todas(page, rcs, cancelar=None):
    """Consulta cada RC (2 tentativas) e grava no Supabase. Retorna (itens_gravados, erros, interrompido)."""
    total, erros = 0, []
    for rc in rcs:
        if cancelar and cancelar():
            return total, erros, True
        # Tenta até 2 vezes por RC: se houver falha, recarrega o chat do zero
        for tentativa_rc in range(2):
            try:
                if tentativa_rc > 0:
                    print(f"RC {rc}: recarregando o chat e tentando novamente...")
                    abrir_chat(page)
                    time.sleep(4)

                texto = consultar_rc_inteligente(page, rc)
                rows = parse_blocks(texto, only_rc=rc)
                if not rows:
                    raise ValueError("bot respondeu, mas nenhum item foi reconhecido")

                gravar(rows)
                total += len(rows)
                print(f"RC {rc}: {len(rows)} itens gravados com sucesso!")
                break
            except SessaoExpirada:
                raise
            except Exception as e:
                print(f"RC {rc} (tentativa {tentativa_rc + 1}): ERRO -> {e}", file=sys.stderr)
                if tentativa_rc == 1:
                    foto(page, f"rc_{rc}")
                    erros.append(f"{rc}: {e}")
                    traceback.print_exc()
        time.sleep(3)
    return total, erros, False


def sincronizar(rcs=None, projeto_id=None, local=False, origem="github", cancelar=None):
    """Uma sincronização completa. Sempre fecha o registro em exiros_sync_log (mesmo se algo quebrar).
    Retorna {"status", "total", "mensagem"}; status: ok | parcial | erro | sessao_expirada."""
    rcs = [r for r in (rcs or []) if r] or listar_rcs(projeto_id)

    # projeto_id no log = esta execução cobre só esse projeto (nulo = todos); o app usa isso para mostrar o status de cada projeto
    log = {"status": "executando", "origem": origem}
    if projeto_id:
        log["projeto_id"] = projeto_id
    log_id = rest("POST", "exiros_sync_log", log, prefer="return=representation")[0]["id"]
    if not rcs:
        msg = "Nenhuma RC encontrada em suprimentos.rc_numero" + (f" para o projeto {projeto_id}" if projeto_id else "")
        log_fim(log_id, "erro", 0, 0, msg)
        return {"status": "erro", "total": 0, "mensagem": msg}

    state = None
    if not local:
        state = load_state()
        if not state:
            msg = "Sem sessão salva. Rode login_local.py"
            log_fim(log_id, "sessao_expirada", len(rcs), 0, msg)
            return {"status": "sessao_expirada", "total": 0, "mensagem": msg}

    total, erros, status, msg = 0, [], "erro", ""
    try:
        with sync_playwright() as p:
            browser = ctx = None
            try:
                browser, ctx = abrir_contexto(p, local, state)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    abrir_chat(page)
                    total, erros, interrompido = _consultar_todas(page, rcs, cancelar)
                    if not local:
                        # indexed_db=True: mantém o login do Teams (que também vive no IndexedDB) renovado
                        save_state(ctx.storage_state(indexed_db=True))
                    status = "ok" if not erros else ("parcial" if total else "erro")
                    msg = "; ".join(erros)
                    if interrompido:
                        status = "parcial" if total else "erro"
                        msg = "Interrompido pelo administrador. " + msg
                except SessaoExpirada as e:
                    foto(page, "sessao_expirada")
                    status, msg = "sessao_expirada", str(e)
                except Exception as e:
                    foto(page, "erro_geral")
                    traceback.print_exc()
                    status, msg = "erro", f"{e}"
            finally:
                for obj in (ctx, browser):
                    try:
                        if obj:
                            obj.close()
                    except Exception:
                        pass
    except Exception as e:  # navegador nem abriu (Chrome ausente, perfil em uso etc.)
        traceback.print_exc()
        status, msg = "erro", f"falha ao abrir o navegador: {e}"

    log_fim(log_id, status, len(rcs), total, msg)
    return {"status": status, "total": total, "mensagem": msg}


# ---------- linha de comando (GitHub Actions e testes) ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rc", action="append", help="RC específica (pode repetir)")
    ap.add_argument("--projeto", help="id do projeto: atualiza só as RCs dele (vazio = todos)")
    ap.add_argument("--local", action="store_true", help="usa o Chrome deste PC em vez do modo GitHub")
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

    r = sincronizar(rcs, projeto_id, local=args.local, origem="local" if args.local else "github")
    print(f"Resultado: {r['status']} · {r['total']} itens · {r['mensagem']}")
    return {"ok": 0, "sessao_expirada": 2}.get(r["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
