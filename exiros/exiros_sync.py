def consultar_rc(page, rc):
    # 1. Você inicia a conversa mandando "Olá"
    enviar(page, "Olá")
    
    # 2. O bot responde com as opções, nós enviamos "status de documentos"
    passo(page, "Como posso te ajudar", lambda: enviar(page, "status de documentos"))
    
    # 3. O bot pergunta o tipo, nós enviamos "rc"
    passo(page, "Qual tipo de documento", lambda: enviar(page, "rc"))
    
    # 4. O bot pede o número da RC, nós enviamos o número real da RC
    passo(page, "Digite o número da sua RC", lambda: enviar(page, rc))

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
