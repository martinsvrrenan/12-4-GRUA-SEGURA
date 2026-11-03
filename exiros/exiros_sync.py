"""
Sincronização Exiros/Teams -> Supabase.
Mantém exiros_common.py inalterado.
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

MESSAGE_SELECTORS = (
    '[data-tid="message"]',
    '[data-tid*="message-container"]',
    '[data-testid*="message"]',
    '[role="listitem"]',
    '.message-group',
)
POLL_SECONDS = 1.0
RESPONSE_TIMEOUT = int(os.environ.get("EXIROS_RESPONSE_TIMEOUT", "45"))
MAX_STEPS = 14


class SessaoExpirada(RuntimeError):
    pass


class FluxoInesperado(RuntimeError):
    pass


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


def abrir_chat(page):
    page.goto(CHAT_URL or TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
    deadline = time.time() + 90
    clicked = bool(CHAT_URL)

    while time.time() < deadline:
        if re.search(r"login\.(microsoftonline|live)\.com", page.url, re.I):
            raise SessaoExpirada("Teams solicitou novo login. Atualize a sessão pelo login_local.py.")
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

    raise TimeoutError("Não foi possível abrir o chat/campo de mensagem do Exiros.")


def _message_nodes(page):
    found = []
    seen = set()
    for selector in MESSAGE_SELECTORS:
        try:
            nodes = page.locator(selector)
            count = min(nodes.count(), 100)
            for i in range(count):
                node = nodes.nth(i)
                txt = _visible_text(node)
                if not txt or txt in seen:
                    continue
                if len(txt) > 12000:
                    continue
                seen.add(txt)
                found.append(txt)
        except Exception:
            continue
    return found[-40:]


def _all_chat_text(page):
    messages = _message_nodes(page)
    if messages:
        return "\n\n".join(messages)
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
    messages = _message_nodes(page)
    return messages[-1] if messages else ""


def _signature(page):
    messages = _message_nodes(page)
    return tuple(messages[-5:])


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
            f"ULTIMAS MENSAGENS:\n" + "\n\n---\n\n".join(_message_nodes(page)[-20:]) +
            "\n\nTEXTO CHAT:\n" + _all_chat_text(page)[-12000:],
            encoding="utf-8"
        )
    except Exception:
        pass


def enviar(page, texto):
    box = page.locator(TEXTBOX).last
    if not box.count() or not box.is_visible():
        raise FluxoInesperado("Campo de mensagem não está visível.")
    box.click()
    page.keyboard.insert_text(str(texto))
    page.keyboard.press("Enter")


def clicar_opcao_na_ultima_mensagem(page, padrao):
    rx = re.compile(padrao, re.I)
    messages = page.locator(
        '[data-tid="message"], [data-tid*="message-container"], '
        '[data-testid*="message"], [role="listitem"]'
    )
    try:
        for i in range(messages.count() - 1, max(-1, messages.count() - 6), -1):
            msg = messages.nth(i)
            try:
                if not msg.is_visible():
                    continue
                buttons = msg.get_by_role("button", name=rx)
                if buttons.count():
                    btn = buttons.last
                    if btn.is_visible() and btn.is_enabled():
                        btn.click(timeout=5000)
                        return True
            except Exception:
                continue
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name=rx).last
        if btn.count() and btn.is_visible() and btn.is_enabled():
            btn.click(timeout=5000)
            return True
    except Exception:
        pass
    return False


def aguardar_resposta_nova(page, assinatura_anterior, texto_enviado=None, timeout=RESPONSE_TIMEOUT):
    deadline = time.time() + timeout
    estavel = None
    repeticoes = 0

    while time.time() < deadline:
        atual = _signature(page)
        if atual and atual != assinatura_anterior:
            ultima = atual[-1]
            if texto_enviado and norm(ultima) == norm(texto_enviado):
                time.sleep(POLL_SECONDS)
                continue
            if atual == estavel:
                repeticoes += 1
            else:
                estavel, repeticoes = atual, 0
            if repeticoes >= 2:
                return ultima
        time.sleep(POLL_SECONDS)

    raise PlaywrightTimeoutError(
        f"Não apareceu resposta nova do Exiros em {timeout}s após a ação."
    )


def _estado_da_mensagem(texto):
    t = norm(texto)
    if any(x in t for x in ("qual tipo de documento", "tipo de documento deseja", "document type")):
        return "TIPO_DOCUMENTO"
    if any(x in t for x in ("digite o numero da sua rc", "informe o numero da rc",
                            "numero da rc", "enter your rc", "rc number")):
        return "NUMERO_RC"
    if any(x in t for x in ("digite o numero do item", "numero do item", "item number",
                            "selecione os itens", "deseja consultar todos")):
        return "ITENS"
    if FIM_ITEM.lower() in t:
        return "RESULTADO"
    if any(x in t for x in ("como posso te ajudar", "como posso ajudar",
                            "select one of the options", "bem-vindo novamente")):
        return "MENU"
    if any(x in t for x in ("desculpe", "nao entendi", "não entendi")):
        return "ERRO_BOT"
    return "OUTRO"


def _acao_menu(page):
    if not clicar_opcao_na_ultima_mensagem(page, r"^\s*Status de documentos\s*$"):
        enviar(page, "Status de documentos")


def _acao_tipo_documento(page):
    if not clicar_opcao_na_ultima_mensagem(page, r"^\s*RC\s*$"):
        enviar(page, "RC")


def consultar_rc(page, rc):
    inicio = time.time()
    passos = 0
    ultima_acao = None
    mensagem = _latest_message(page)

    if not mensagem:
        antes = _signature(page)
        enviar(page, "Olá")
        mensagem = aguardar_resposta_nova(page, antes, "Olá")

    while passos < MAX_STEPS and time.time() - inicio < 240:
        passos += 1
        estado = _estado_da_mensagem(mensagem)

        if estado == "RESULTADO":
            texto = _all_chat_text(page)
            return texto

        if estado == "MENU":
            acao = "Status de documentos"
            if ultima_acao == acao:
                raise FluxoInesperado("O Exiros repetiu o menu após selecionar Status de documentos.")
            antes = _signature(page)
            _acao_menu(page)
            ultima_acao = acao
            mensagem = aguardar_resposta_nova(page, antes, None)
            continue

        if estado == "TIPO_DOCUMENTO":
            acao = "RC"
            if ultima_acao == acao:
                raise FluxoInesperado("O Exiros repetiu a pergunta de tipo de documento após selecionar RC.")
            antes = _signature(page)
            _acao_tipo_documento(page)
            ultima_acao = acao
            mensagem = aguardar_resposta_nova(page, antes, acao)
            continue

        if estado == "NUMERO_RC":
            antes = _signature(page)
            enviar(page, rc)
            ultima_acao = rc
            mensagem = aguardar_resposta_nova(page, antes, rc)
            continue

        if estado == "ITENS":
            antes = _signature(page)
            if not clicar_opcao_na_ultima_mensagem(page, r"^\s*Todos\s*$"):
                enviar(page, "Todos")
            ultima_acao = "Todos"
            mensagem = aguardar_resposta_nova(page, antes, "Todos")
            continue

        if estado == "ERRO_BOT":
            raise FluxoInesperado(f"Exiros informou que não entendeu a solicitação: {mensagem[:500]}")

        antes = _signature(page)
        try:
            mensagem = aguardar_resposta_nova(page, antes, timeout=12)
        except PlaywrightTimeoutError:
            raise FluxoInesperado(
                "Estado do Exiros não reconhecido. "
                f"Última mensagem: {mensagem[:500]}"
            )

    raise FluxoInesperado("Limite de etapas/tempo excedido sem concluir a consulta.")


def listar_rcs():
    rows = rest("GET", "suprimentos?select=rc_numero&rc_numero=not.is.null")
    rcs = {re.sub(r"\D", "", row.get("rc_numero") or "") for row in rows}
    return sorted(value for value in rcs if value)


def gravar(rows):
    if rows:
        rest("POST", "exiros_rc?on_conflict=rc_item", rows,
             prefer="resolution=merge-duplicates,return=minimal")


def log_fim(log_id, status, rcs, itens, msg):
    rest("PATCH", f"exiros_sync_log?id=eq.{log_id}",
         {"finalizado_em": agora(), "status": status,
          "rcs": rcs, "itens": itens, "mensagem": (msg or "")[:1000]},
         prefer="return=minimal")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rc", action="append", help="RC específica (pode repetir)")
    ap.add_argument("--headed", action="store_true", help="Abre navegador visível para diagnóstico")
    args = ap.parse_args()

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
        ctx = browser.new_context(storage_state=state, **CONTEXT_OPTS)
        page = ctx.new_page()
        try:
            abrir_chat(page)
        except SessaoExpirada as exc:
            foto(page, "sessao_expirada", str(exc))
            log_fim(log_id, "sessao_expirada", len(rcs), 0, str(exc))
            browser.close()
            return 2
        except Exception as exc:
            foto(page, "erro_abrir_chat", str(exc))
            log_fim(log_id, "erro", len(rcs), 0, f"Não foi possível abrir o chat: {exc}")
            browser.close()
            return 1

        for rc in rcs:
            try:
                print(f"Consultando RC {rc}...")
                texto = consultar_rc(page, rc)
                rows = parse_blocks(texto, only_rc=rc)
                problemas = validar_registros(rows, only_rc=rc)
                if problemas:
                    raise ValueError("Validação dos dados falhou: " + "; ".join(problemas[:8]))
                if not rows:
                    raise ValueError("Resposta recebida, mas nenhum item válido foi identificado.")
                gravar(rows)
                total += len(rows)
                print(f"RC {rc}: {len(rows)} item(ns) gravado(s).")
                
                antes = _signature(page)
                enviar(page, "Menu")
                try:
                    aguardar_resposta_nova(page, antes, "Menu", timeout=12)
                except Exception:
                    pass
            except Exception as exc:
                erro = f"{rc}: {type(exc).__name__}: {exc}"
                print(erro, file=sys.stderr)
                foto(page, f"rc_{rc}", erro + "\n" + traceback.format_exc())
                erros.append(erro)
                # Mantém o fluxo ativo para as próximas RCs mesmo se uma falhar
                continue

        try:
            save_state(ctx.storage_state())
        except Exception as exc:
            erros.append(f"Falha ao salvar sessão: {exc}")
        browser.close()

    status = "ok" if not erros else ("parcial" if total else "erro")
    log_fim(log_id, status, len(rcs), total, "; ".join(erros))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
