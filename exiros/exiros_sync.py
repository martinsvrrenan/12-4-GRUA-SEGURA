# Substitui a função consultar_rc do exiros_sync.py (e adiciona os 3 helpers abaixo dela).
# No topo do exiros_sync.py garanta: import re, time, unicodedata
#
# O que muda:
#  1) Os textos esperados do bot são comparados sem acento, sem maiúsculas e com variações
#     ("Digite o número da sua RC" / "Informe o número da RC" etc.).
#  2) Se o bot não responder o esperado, o erro gravado no log do app passa a trazer as
#     últimas mensagens do chat, para vermos o que o bot realmente disse.
import re
import time
import unicodedata


def _norm(t):
    t = unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower())


def _conta(page, padrao):
    return len(re.findall(padrao, _norm(chat_text(page))))


def enviar_e_esperar(page, texto, padrao, timeout=60):
    """Envia `texto` e espera uma NOVA ocorrência de `padrao` (regex em minúsculas, sem acento)."""
    antes = _conta(page, padrao)
    enviar(page, texto)
    fim = time.time() + timeout
    while time.time() < fim:
        if _conta(page, padrao) > antes:
            return
        time.sleep(1)
    cauda = re.sub(r"\s+", " ", chat_text(page)[-500:]).strip()
    raise TimeoutError(f"Depois de enviar '{texto}' o bot não respondeu /{padrao}/. Últimas mensagens do chat: ...{cauda}")


def consultar_rc(page, rc):
    # 1. inicia a conversa
    enviar_e_esperar(page, "Olá", r"como posso (te )?ajudar")

    # 2. escolhe "status de documentos"
    enviar_e_esperar(page, "status de documentos", r"qual (o )?tipo( de documento)?")

    # 3. escolhe o tipo "rc" e espera o bot pedir o número
    enviar_e_esperar(page, "rc", r"(digite|informe|insira|envie)[^.?!]{0,40}numero|numero (da|de|do)[^.?!]{0,15}\brc\b")

    # 4. envia o número real da RC
    enviar(page, rc)

    antes_ok = chat_text(page).count("Perfeito, aguarde")
    antes_fim = chat_text(page).count(FIM_ITEM)
    enviar(page, "Todos")
    aguardar_aumento(page, "Perfeito, aguarde", antes_ok, 60)

    # espera os itens chegarem e o número parar de crescer por ~8 s
    fim, ultimo, desde = time.time() + 150, -1, time.time()
    while time.time() < fim:
        texto = chat_text(page)
        n = texto.count(FIM_ITEM)
        if n > antes_fim:
            if n != ultimo:
                ultimo, desde = n, time.time()
            elif time.time() - desde >= 8:
                return texto
        time.sleep(1.5)
    raise TimeoutError("O bot não devolveu os itens a tempo")
