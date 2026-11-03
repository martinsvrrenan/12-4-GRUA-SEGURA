"""
Sincronização Exiros/Teams -> Supabase.
Mantém exiros_common.py inalterado.

Uso:
  python exiros_sync.py                 # todas as RCs de suprimentos.rc_numero
  python exiros_sync.py --rc 33983723   # RC específica (pode repetir)
  python exiros_sync.py --headed        # navegador visível
  python exiros_sync.py --diag          # diagnóstico: botões, seletor e HTML do chat
                                        # (sem terminal, ex. GitHub Actions, roda headless sozinho)

Fluxo do bot (cards do Teams, só aceitam CLIQUE em botão):
  Menu -> [Status de documentos] -> [RC] -> digitar nº da RC -> [Todos] -> resultado
"""
import argparse
import os
import re
import sys
import time
import traceback
import unicodedata
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from exiros_common import (
    CONTEXT_OPTS, TEAMS_URL, agora, load_state, rest, save_state
)
from exiros_parser import parse_blocks, validar_registros

BOT_NAME = os.environ.get("EXIROS_BOT_NAME", "Exiros")
CHAT_URL = os.environ.get("TEAMS_CHAT_URL", "").strip()
TEXTBOX = '[data-tid="ckeditor"], div[role="textbox"][contenteditable="true"]'
DEBUG_DIR = Path(__file__).resolve().parent / "debug"
FIM_ITEM = "Solicitar ação sobre este requerimento"

# Um único seletor por vez (o primeiro que encontrar mensagens). Misturar vários
# duplica textos e embaralha a ordem das mensagens.
MESSAGE_SELECTORS = (
    '[data-tid="chat-pane-message"]',
    '[data-tid="message"]',
    '[data-tid*="message-container"]',
    '[data-testid*="message"]',
    '[role="listitem"]',
)

# Botões do fluxo (nome exato).
BOTOES = {
    "menu": r"^\s*Status de documentos\s*$",
    "tipo": r"^\s*RC\s*$",
    "itens": r"^\s*Todos\s*$",
}

POLL_SECONDS = 1.0
RESPONSE_TIMEOUT = int(os.environ.get("EXIROS_RESPONSE_TIMEOUT", "45"))
LIMITE_RC = 240          # segundos máximos por RC
SETTLE_SECONDS = 6       # tempo sem mensagens novas para considerar o resultado completo


class SessaoExpirada(RuntimeError):
    pass


class FluxoInesperado(RuntimeError):
    pass


# --------------------------------------------------------------------------- utilidades

def norm(value):
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", value).strip().lower()


def _visible_text(locator):
    try:
        if locator.is_visible():
            return locator.inner_text(timeout=1500).strip()
    except Exception:
        pass
    return ""


def _msg_locator(page):
    for sel in MESSAGE_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def _message_nodes(page, limite=40):
    """Textos das últimas `limite` mensagens, na ordem do chat (sem deduplicar)."""
    loc = _msg_locator(page)
    if loc is None:
        return []
    out = []
    try:
        total = loc.count()
        for i in range(max(0, total - limite), total):
            txt = _visible_text(loc.nth(i))
            if txt and len(txt) <= 20000:
                out.append(txt)
    except Exception:
        pass
    return out


def _all_chat_text(page, limite=120):
    msgs = _message_nodes(page, limite)
    if msgs:
        return "\n\n".join(msgs)
    for selector in ('[data-tid="message-pane-list-viewport"]',
                     '[data-tid="chat-pane-list"]', '[role="main"]'):
        try:
            loc = page.locator(selector)
            if loc.count():
                txt = loc.first.inner_text(timeout=2500)
                if txt.strip():
                    return txt
        except Exception:
            pass
    return ""


def _latest_message(page):
    msgs = _message_nodes(page, 1)
    return msgs[-1] if msgs else ""


def _signature(page):
    return tuple(_message_nodes(page, 5))


def _eh_eco(ultima, enviado):
    """True se a mensagem é só o eco do que NÓS enviamos (inclui autor/hora)."""
    if not enviado:
        return False
    return norm(enviado) in norm(ultima) and len(ultima) < len(enviado) + 80


def foto(page, nome, extra=""):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", nome)[:100]
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=False)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{safe}.txt").write_text(
            f"URL: {page.url}\nData: {agora()}\n{extra}\n\n"
            f"BOTOES: {_botao_mais_baixo(page)}\n\n"
            f"CORPO DA PAGINA (início):\n{_body_snippet(page)}\n\n"
            f"ULTIMAS MENSAGENS:\n" + "\n\n---\n\n".join(_message_nodes(page, 20)) +
            "\n\nTEXTO CHAT:\n" + _all_chat_text(page)[-12000:],
            encoding="utf-8"
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- Teams

RE_LOGIN = re.compile(r"login\.(microsoftonline|live|microsoft)\.com|/login|oauth2", re.I)
RE_TELAS_INTERMEDIARIAS = re.compile(
    r"Use the web app|Usar o aplicativo Web|Usar a vers[aã]o web|"
    r"Continue on this browser|Continuar neste navegador|Continuar aqui", re.I)


def _body_snippet(page, n=3000):
    try:
        return page.locator("body").inner_text(timeout=3000)[:n]
    except Exception:
        return ""


def _titulo(page):
    try:
        return page.title()
    except Exception:
        return ""


def abrir_chat(page):
    destino = CHAT_URL or TEAMS_URL
    print(f"[abrir_chat] abrindo {destino}")
    page.goto(destino, wait_until="domcontentloaded", timeout=60000)
    deadline = time.time() + 120
    clicked = bool(CHAT_URL)
    ultimo_log = 0

    while time.time() < deadline:
        if RE_LOGIN.search(page.url):
            raise SessaoExpirada("Teams solicitou novo login. Atualize a sessão pelo login_local.py.")

        if time.time() - ultimo_log > 15:
            print(f"[abrir_chat] url={page.url} titulo={_titulo(page)!r}")
            ultimo_log = time.time()

        # Telas do tipo "Usar o aplicativo Web" / "Continuar neste navegador"
        try:
            alvo = (page.get_by_role("button", name=RE_TELAS_INTERMEDIARIAS)
                    .or_(page.get_by_role("link", name=RE_TELAS_INTERMEDIARIAS)))
            if alvo.count() and alvo.first.is_visible():
                alvo.first.click(timeout=2500)
                time.sleep(2)
                continue
        except Exception:
            pass

        if page.locator(TEXTBOX).count():
            try:
                if page.locator(TEXTBOX).last.is_visible():
                    return
            except Exception:
                pass

        if not clicked:
            for locator in (
                page.get_by_role("treeitem", name=re.compile(BOT_NAME, re.I)),
                page.get_by_text(re.compile(BOT_NAME, re.I)),
            ):
                try:
                    if locator.count() and locator.first.is_visible():
                        locator.first.click(timeout=2500)
                        clicked = True
                        break
                except Exception:
                    continue
        time.sleep(1.5)

    raise TimeoutError(
        "Não foi possível abrir o chat/campo de mensagem do Exiros. "
        f"URL final: {page.url} | título: {_titulo(page)!r} | "
        f"TEAMS_CHAT_URL definido: {bool(CHAT_URL)}"
    )


def enviar(page, texto):
    box = page.locator(TEXTBOX).last
    if not box.count() or not box.is_visible():
        raise FluxoInesperado("Campo de mensagem não está visível.")
    box.click()
    page.keyboard.insert_text(str(texto))
    page.keyboard.press("Enter")


def _locator_botao(page, padrao):
    rx = re.compile(padrao, re.I)
    return page.get_by_role("button", name=rx).or_(
        page.locator('button, [role="button"], a').filter(has_text=rx)
    )


def _botao_mais_baixo(page):
    """Chave do botão do fluxo mais baixo na tela = o do card mais recente."""
    melhor, melhor_y = None, -1
    for chave, padrao in BOTOES.items():
        try:
            loc = _locator_botao(page, padrao)
            n = loc.count()
            for i in range(max(0, n - 5), n):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if box and box["y"] > melhor_y:
                    melhor, melhor_y = chave, box["y"]
        except Exception:
            continue
    return melhor


def _clicar(page, padrao):
    try:
        loc = _locator_botao(page, padrao)
        n = loc.count()
        for i in range(n - 1, max(-1, n - 6), -1):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_enabled():
                    el.scroll_into_view_if_needed(timeout=3000)
                    el.click(timeout=5000)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _clicar_ou_falhar(page, chave):
    """Clica no botão; se não achar, NÃO digita nada (o bot rejeita texto)."""
    if not _clicar(page, BOTOES[chave]):
        foto(page, f"botao_{chave}", "Botão não encontrado")
        raise FluxoInesperado(f"Botão '{chave}' não encontrado; não vou digitar texto.")


def _esperar(condicao, timeout, descricao):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if condicao():
                return
        except Exception:
            pass
        time.sleep(POLL_SECONDS)
    raise PlaywrightTimeoutError(f"Tempo esgotado ({timeout}s) esperando: {descricao}")


# --------------------------------------------------------------------------- fluxo do bot

def voltar_ao_menu(page):
    """Sempre começa do zero: envia 'Menu' e espera o card do menu novo."""
    antes = _signature(page)
    enviar(page, "Menu")

    def menu_novo():
        return (_signature(page) != antes
                and not _eh_eco(_latest_message(page), "Menu")
                and _botao_mais_baixo(page) == "menu")

    try:
        _esperar(menu_novo, 25, "menu do bot")
    except PlaywrightTimeoutError:
        foto(page, "menu_nao_apareceu")
        raise FluxoInesperado("O menu do bot não apareceu após enviar 'Menu'.")
    time.sleep(1.0)


def _resultado_pronto(page, rc_int):
    """Resultado novo: as últimas mensagens trazem 'RC: <rc>-N' e o fim do item."""
    recente = "\n".join(_message_nodes(page, 3))
    return (re.search(rf"RC:\s*0*{rc_int}-\d+", recente, re.I) is not None
            and norm(FIM_ITEM) in norm(recente))


def consultar_rc(page, rc):
    inicio = time.time()
    rc_int = int(rc)

    def restante():
        return max(5, LIMITE_RC - (time.time() - inicio))

    # 1) Menu -> Status de documentos
    voltar_ao_menu(page)
    _clicar_ou_falhar(page, "menu")
    try:
        _esperar(lambda: _botao_mais_baixo(page) == "tipo", min(RESPONSE_TIMEOUT, restante()),
                 "tela de tipo de documento (botão RC)")
    except PlaywrightTimeoutError:
        foto(page, f"rc_{rc}_tipo", "Tela de tipo de documento não apareceu")
        raise FluxoInesperado("Tela de tipo de documento não apareceu após 'Status de documentos'.")

    # 2) Tipo = RC
    antes = _signature(page)
    _clicar_ou_falhar(page, "tipo")

    # 3) Esperar o bot pedir o número da RC (mensagem nova, que não seja eco do clique)
    try:
        _esperar(lambda: _signature(page) != antes
                 and not _eh_eco(_latest_message(page), "RC"),
                 min(RESPONSE_TIMEOUT, restante()), "pedido do número da RC")
    except PlaywrightTimeoutError:
        foto(page, f"rc_{rc}_pedido_numero")
        raise FluxoInesperado("O bot não pediu o número da RC após selecionar 'RC'.")
    time.sleep(1.0)

    # 4) Digitar o número da RC (único ponto em que se digita)
    antes = _signature(page)
    enviar(page, rc)

    # 5) Esperar o card de itens ([Todos]) ou, em RC de item único, o resultado direto
    def itens_ou_resultado():
        return _botao_mais_baixo(page) == "itens" or _resultado_pronto(page, rc_int)

    try:
        _esperar(lambda: _signature(page) != antes and itens_ou_resultado(),
                 min(RESPONSE_TIMEOUT, restante()), "opção 'Todos' ou resultado")
    except PlaywrightTimeoutError:
        foto(page, f"rc_{rc}_itens")
        raise FluxoInesperado("O bot não apresentou a opção 'Todos' nem o resultado.")

    if not _resultado_pronto(page, rc_int):
        _clicar_ou_falhar(page, "itens")
        try:
            _esperar(lambda: _resultado_pronto(page, rc_int),
                     min(RESPONSE_TIMEOUT, restante()), "resultado da RC")
        except PlaywrightTimeoutError:
            foto(page, f"rc_{rc}_resultado")
            raise FluxoInesperado("O resultado da RC não apareceu após 'Todos'.")

    # 6) Esperar o bot terminar de enviar todos os itens
    ref, desde = _signature(page), time.time()
    while time.time() - desde < SETTLE_SECONDS and time.time() - inicio < LIMITE_RC:
        time.sleep(1)
        atual = _signature(page)
        if atual != ref:
            ref, desde = atual, time.time()

    return _all_chat_text(page)


# --------------------------------------------------------------------------- Supabase

def listar_rcs():
    rcs, offset, passo = set(), 0, 1000
    while True:
        rows = rest(
            "GET",
            f"suprimentos?select=rc_numero&rc_numero=not.is.null&order=rc_numero"
            f"&limit={passo}&offset={offset}"
        )
        if not rows:
            break
        for row in rows:
            # Primeira sequência de 5+ dígitos (evita juntar "123456-10" em 12345610)
            m = re.search(r"\d{5,}", str(row.get("rc_numero") or ""))
            if m:
                rcs.add(m.group(0))
        if len(rows) < passo:
            break
        offset += passo
    return sorted(rcs)


def gravar(rows):
    if rows:
        rest("POST", "exiros_rc?on_conflict=rc_item", rows,
             prefer="resolution=merge-duplicates,return=minimal")


def log_fim(log_id, status, rcs, itens, msg):
    rest("PATCH", f"exiros_sync_log?id=eq.{log_id}",
         {"finalizado_em": agora(), "status": status,
          "rcs": rcs, "itens": itens, "mensagem": (msg or "")[:1000]},
         prefer="return=minimal")


# --------------------------------------------------------------------------- diagnóstico

def diagnostico(page):
    abrir_chat(page)
    if sys.stdin.isatty():
        input("Deixe o menu do bot visível no navegador e aperte Enter... ")
    else:
        # Sem terminal (ex.: GitHub Actions): pede o menu ao bot e espera.
        try:
            enviar(page, "Menu")
        except Exception as exc:
            print("Não consegui enviar 'Menu':", exc)
        time.sleep(10)
    print("Seletor de mensagens:", next(
        (s for s in MESSAGE_SELECTORS if page.locator(s).count()), None))
    print("Mensagens (últimas 5):")
    for m in _message_nodes(page, 5):
        print("  -", m[:120].replace("\n", " | "))
    print("Botão mais baixo do fluxo:", _botao_mais_baixo(page))
    print("Botões (role=button):", page.get_by_role("button").all_inner_texts()[-15:])
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        html = page.locator('[role="main"]').first.inner_html()
        (DEBUG_DIR / "chat_dump.html").write_text(html, encoding="utf-8")
        print(f"HTML salvo em {DEBUG_DIR / 'chat_dump.html'}")
    except Exception as exc:
        print("Falha ao salvar HTML:", exc)
    foto(page, "diagnostico")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rc", action="append", help="RC específica (pode repetir)")
    ap.add_argument("--headed", action="store_true", help="Abre navegador visível")
    ap.add_argument("--diag", action="store_true", help="Modo diagnóstico (implica --headed)")
    args = ap.parse_args()

    if args.diag:
        state = load_state()
        if not state:
            print("Sem sessão salva. Execute login_local.py.", file=sys.stderr)
            return 2
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not sys.stdin.isatty())
            ctx = browser.new_context(storage_state=state, **CONTEXT_OPTS)
            diagnostico(ctx.new_page())
            browser.close()
        return 0

    rcs = [re.sub(r"\D", "", value) for value in (args.rc or [])]
    if not rcs and os.environ.get("RC_INPUT", "").strip():
        rcs = [re.sub(r"\D", "", value) for value in
               re.split(r"[,; ]+", os.environ["RC_INPUT"]) if value.strip()]
    rcs = [value for value in rcs if value] or listar_rcs()

    log_id = rest("POST", "exiros_sync_log", {"status": "executando"},
                  prefer="return=representation")[0]["id"]
    if not rcs:
        log_fim(log_id, "erro", 0, 0, "Nenhuma RC encontrada em suprimentos.rc_numero")
        return 1

    state = load_state()
    if not state:
        log_fim(log_id, "sessao_expirada", len(rcs), 0,
                "Sem sessão salva. Execute login_local.py para renovar a sessão.")
        return 2

    total, erros = 0, []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            ctx = browser.new_context(storage_state=state, **CONTEXT_OPTS)
            page = ctx.new_page()
            try:
                abrir_chat(page)
            except SessaoExpirada as exc:
                foto(page, "sessao_expirada", str(exc))
                log_fim(log_id, "sessao_expirada", len(rcs), 0, str(exc))
                return 2
            except Exception as exc:
                foto(page, "erro_abrir_chat", str(exc))
                log_fim(log_id, "erro", len(rcs), 0, f"Não foi possível abrir o chat: {exc}")
                return 1

            for rc in rcs:
                try:
                    print(f"Consultando RC {rc}...")
                    texto = consultar_rc(page, rc)
                    rows = parse_blocks(texto, only_rc=rc)
                    problemas = validar_registros(rows, only_rc=rc)
                    if problemas:
                        raise ValueError("Validação dos dados falhou: " + "; ".join(problemas[:8]))
                    gravar(rows)
                    total += len(rows)
                    print(f"RC {rc}: {len(rows)} item(ns) gravado(s).")
                except Exception as exc:
                    erro = f"{rc}: {type(exc).__name__}: {exc}"
                    print(erro, file=sys.stderr)
                    foto(page, f"rc_{rc}", erro + "\n" + traceback.format_exc())
                    erros.append(erro)
                    # A próxima RC recomeça pelo menu (voltar_ao_menu), então
                    # não é preciso limpar nada aqui.

            try:
                save_state(ctx.storage_state())
            except Exception as exc:
                erros.append(f"Falha ao salvar sessão: {exc}")
        finally:
            browser.close()

    status = "ok" if not erros else ("parcial" if total else "erro")
    log_fim(log_id, status, len(rcs), total, "; ".join(erros))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
