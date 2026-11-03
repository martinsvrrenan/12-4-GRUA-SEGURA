"""Converte respostas do Exiros (Teams) em linhas para public.exiros_rc.

Mantém os nomes das colunas usados pela versão anterior.
A validação impede que uma resposta de outra RC ou um bloco sem campos úteis
seja enviado ao upsert.
"""
import re
from datetime import datetime, timezone

MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

CHAVES = [
    "Status da licitação", "Data de abertura da licitação", "Data de encerramento da licitação",
    "Número da licitação", "Usuário solicitante", "Data de necessidade", "Quantidade requerida",
    "Ordem de serviço", "RC Exiros", "Data de criação da RC", "Data de liberação da RC",
    "Comprador", "Planejador", "Material", "Holding", "Planta", "Status",
]
RE_CHAVE = re.compile(r"^\s*(" + "|".join(re.escape(c) for c in CHAVES) + r"):[ \t]*(.*)$", re.I)
RE_RC = re.compile(r"(?<![A-Za-zÀ-ÿ])RC:\s*(\d{5,})-(\d+)", re.I)
RE_VAZIO = {"", "-", "–", "—"}


def _vazio(v):
    return v is None or str(v).strip() in RE_VAZIO


def _dash(v):
    """'B63 – Nome' -> ('B63', 'Nome')."""
    if _vazio(v):
        return None, None
    partes = re.split(r"\s+[–—-]\s+", str(v).strip(), maxsplit=1)
    return partes[0].strip() or None, (partes[1].strip() or None) if len(partes) > 1 else None


def _data(v):
    if _vazio(v):
        return None
    valor = str(v).strip()
    m = re.search(r"(\d{1,2})\s+([a-zçA-Z]{3})\.?\s+(\d{4})", valor, re.I)
    if m:
        mes = MESES.get(m.group(2).lower())
        if mes:
            try:
                return datetime(int(m.group(3)), mes, int(m.group(1))).date().isoformat()
            except ValueError:
                return None
    # Também aceita datas ISO já devolvidas pelo bot.
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", valor)
    if m:
        try:
            return datetime(*map(int, m.groups())).date().isoformat()
        except ValueError:
            return None
    return None


def _num(v):
    if _vazio(v):
        return None
    s = str(v).strip().replace(" ", "")
    # Formato brasileiro: 1.234,56. Se só houver vírgula, trata como decimal.
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        numero = float(s)
        return numero if numero == numero and abs(numero) != float("inf") else None
    except (ValueError, TypeError):
        return None


def _txt(v):
    return None if _vazio(v) else str(v).strip()


def _campos(bloco):
    linhas = str(bloco).splitlines()
    campos = {}
    for i, linha in enumerate(linhas):
        m = RE_CHAVE.match(linha)
        if not m:
            continue
        chave, valor = m.group(1), m.group(2)
        # Preserva a grafia canônica da lista CHAVES.
        chave = next((c for c in CHAVES if c.lower() == chave.lower()), chave)
        if not valor.strip() and i + 1 < len(linhas):
            prox = linhas[i + 1].strip()
            if prox and not RE_CHAVE.match(prox) and not prox.startswith(("RC:", "Solicitar ação")):
                valor = prox
        campos[chave] = valor.strip()
    return campos


def parse_blocks(texto, only_rc=None):
    """Retorna um dict por item; a última ocorrência válida de cada item prevalece."""
    texto = texto or ""
    agora = datetime.now(timezone.utc).isoformat()
    marcas = list(RE_RC.finditer(texto))
    itens = {}

    rc_filtro = str(only_rc).strip().lstrip("0") if only_rc is not None else None
    if rc_filtro == "":
        rc_filtro = "0"

    for i, match in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        bloco = texto[match.start():fim]
        rc_original = match.group(1)
        rc_num = rc_original.lstrip("0") or "0"
        item = int(match.group(2))

        if rc_filtro is not None and rc_num != rc_filtro:
            continue

        campos = _campos(bloco)
        # Não cria registros a partir de ocorrências soltas de "RC: número-item".
        if not campos:
            continue

        status_txt = campos.get("Status", "")
        status_match = re.match(r"^(.*?)\s*\((.*)\)\s*$", status_txt)
        status, status_desc = (
            (status_match.group(1).strip(), status_match.group(2).strip())
            if status_match else (_txt(status_txt), None)
        )

        material = campos.get("Material", "")
        material_match = re.match(r"^(\S+)\s+[–—-]\s+(.*?)\s*\(([^()]*)\)\s*$", material)
        if material_match:
            mat_cod = material_match.group(1)
            mat_desc = material_match.group(2).strip() or None
            unidade = material_match.group(3).strip() or None
        else:
            mat_cod, mat_desc = _dash(material)
            unidade = None

        sol_user, sol_nome = _dash(campos.get("Usuário solicitante"))
        comp_cod, comp_nome = _dash(campos.get("Comprador"))

        registro = {
            "rc_item": f"{rc_original}-{match.group(2)}",
            "rc_numero": rc_num,
            "item": item,
            "status": status,
            "status_descricao": status_desc,
            "material_codigo": mat_cod,
            "material_descricao": mat_desc,
            "unidade": unidade,
            "holding": _txt(campos.get("Holding")),
            "planta": _txt(campos.get("Planta")),
            "solicitante_usuario": sol_user,
            "solicitante_nome": sol_nome,
            "data_necessidade": _data(campos.get("Data de necessidade")),
            "quantidade": _num(campos.get("Quantidade requerida")),
            "ordem_servico": _txt(campos.get("Ordem de serviço")),
            "rc_exiros": _txt(campos.get("RC Exiros")),
            "data_criacao_rc": _data(campos.get("Data de criação da RC")),
            "data_liberacao_rc": _data(campos.get("Data de liberação da RC")),
            "comprador_codigo": comp_cod,
            "comprador_nome": comp_nome,
            "planejador": _txt(campos.get("Planejador")),
            "licitacao_numero": _txt(campos.get("Número da licitação")),
            "licitacao_status": _txt(campos.get("Status da licitação")),
            "licitacao_abertura": _data(campos.get("Data de abertura da licitação")),
            "licitacao_encerramento": _data(campos.get("Data de encerramento da licitação")),
            "raw": {"texto": bloco.strip(), "campos": campos},
            "consultado_em": agora,
        }

        chave = registro["rc_item"]
        anterior = itens.get(chave)
        # Um bloco posterior só substitui o anterior se tiver ao menos um campo de negócio.
        qualidade = sum(registro.get(k) is not None for k in
                        ("status", "material_codigo", "material_descricao", "holding",
                         "planta", "quantidade", "data_necessidade", "rc_exiros"))
        qualidade_anterior = sum(anterior.get(k) is not None for k in
                                 ("status", "material_codigo", "material_descricao", "holding",
                                  "planta", "quantidade", "data_necessidade", "rc_exiros")) if anterior else -1
        if qualidade >= qualidade_anterior:
            itens[chave] = registro

    return list(itens.values())


def validar_registros(rows, only_rc=None):
    """Retorna mensagens de validação; lista vazia significa que os dados passaram."""
    problemas = []
    if not rows:
        return ["Nenhum item foi extraído da resposta do Exiros."]

    filtro = str(only_rc).strip().lstrip("0") if only_rc is not None else None
    if filtro == "":
        filtro = "0"

    vistos = set()
    for pos, row in enumerate(rows, start=1):
        rc_num = str(row.get("rc_numero") or "").lstrip("0") or "0"
        if filtro is not None and rc_num != filtro:
            problemas.append(f"Registro {pos} pertence à RC {rc_num}, não à RC {filtro}.")
        if not row.get("rc_item") or row["rc_item"] in vistos:
            problemas.append(f"Registro {pos} possui identificador ausente ou duplicado.")
        vistos.add(row.get("rc_item"))
        if not isinstance(row.get("item"), int) or row["item"] < 0:
            problemas.append(f"Registro {pos} possui número de item inválido.")
        # Um registro sem nenhum campo essencial é sinal de captura parcial.
        if not any(row.get(k) is not None for k in
                   ("status", "material_codigo", "material_descricao", "holding",
                    "planta", "quantidade", "data_necessidade", "rc_exiros")):
            problemas.append(f"Registro {pos} ({row.get('rc_item')}) não contém campos úteis.")
    return problemas
