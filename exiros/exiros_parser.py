"""Converte o texto devolvido pelo Exiros bot (Teams) em linhas para a tabela exiros_rc."""
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
RE_CHAVE = re.compile(r"^\s*(" + "|".join(re.escape(c) for c in CHAVES) + r"):[ \t]*(.*)$")
RE_RC = re.compile(r"(?<![A-Za-zÀ-ÿ])RC:\s*(\d{5,})-(\d+)")
RE_VAZIO = {"", "-", "–", "—"}


def _vazio(v):
    return v is None or v.strip() in RE_VAZIO


def _dash(v):
    """'B63 – Nome' -> ('B63', 'Nome'). Vazio ou '–' -> (None, None)."""
    if _vazio(v):
        return None, None
    partes = re.split(r"\s+[–—-]\s+", v.strip(), maxsplit=1)
    return partes[0].strip(), (partes[1].strip() if len(partes) > 1 else None)


def _data(v):
    if _vazio(v):
        return None
    m = re.search(r"(\d{1,2})\s+([a-zçA-Z]{3})\.?\s+(\d{4})", v)
    if not m:
        return None
    mes = MESES.get(m.group(2).lower())
    if not mes:
        return None
    return f"{int(m.group(3)):04d}-{mes:02d}-{int(m.group(1)):02d}"


def _num(v):
    if _vazio(v):
        return None
    s = v.strip().replace(".", "").replace(",", ".") if "," in v else v.strip().replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def _txt(v):
    return None if _vazio(v) else v.strip()


def _campos(bloco):
    """Lê 'Chave: valor' do bloco. Se o valor vier na linha seguinte, aceita."""
    linhas = [l for l in bloco.splitlines()]
    campos = {}
    for i, linha in enumerate(linhas):
        m = RE_CHAVE.match(linha)
        if not m:
            continue
        chave, valor = m.group(1), m.group(2)
        if not valor.strip() and i + 1 < len(linhas):
            prox = linhas[i + 1].strip()
            if prox and not RE_CHAVE.match(prox) and not prox.startswith(("RC:", "Solicitar ação")):
                valor = prox
        campos[chave] = valor.strip()
    return campos


def parse_blocks(texto, only_rc=None):
    """Retorna uma lista de dicts (um por item de RC), prontos para o upsert.
    Se o mesmo item aparecer mais de uma vez no texto, vale a última ocorrência."""
    agora = datetime.now(timezone.utc).isoformat()
    marcas = list(RE_RC.finditer(texto))
    itens = {}
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        bloco = texto[m.start():fim]
        rc_num = m.group(1).lstrip("0") or "0"
        item = int(m.group(2))
        if only_rc and rc_num != str(only_rc).strip().lstrip("0"):
            continue
        c = _campos(bloco)

        st_txt = c.get("Status", "")
        ms = re.match(r"^(.*?)\s*\((.*)\)\s*$", st_txt)
        status, status_desc = (ms.group(1).strip(), ms.group(2).strip()) if ms else (_txt(st_txt), None)

        mat = c.get("Material", "")
        mm = re.match(r"^(\S+)\s+[–—-]\s+(.*?)\s*\(([^()]*)\)\s*$", mat)
        if mm:
            mat_cod, mat_desc, unid = mm.group(1), mm.group(2).strip(), mm.group(3).strip()
        else:
            mat_cod, mat_desc = _dash(mat)
            unid = None

        sol_user, sol_nome = _dash(c.get("Usuário solicitante"))
        comp_cod, comp_nome = _dash(c.get("Comprador"))

        itens[f"{m.group(1)}-{m.group(2)}"] = {
            "rc_item": f"{m.group(1)}-{m.group(2)}",
            "rc_numero": rc_num,
            "item": item,
            "status": status,
            "status_descricao": status_desc,
            "material_codigo": mat_cod,
            "material_descricao": mat_desc,
            "unidade": unid,
            "holding": _txt(c.get("Holding")),
            "planta": _txt(c.get("Planta")),
            "solicitante_usuario": sol_user,
            "solicitante_nome": sol_nome,
            "data_necessidade": _data(c.get("Data de necessidade")),
            "quantidade": _num(c.get("Quantidade requerida")),
            "ordem_servico": _txt(c.get("Ordem de serviço")),
            "rc_exiros": _txt(c.get("RC Exiros")),
            "data_criacao_rc": _data(c.get("Data de criação da RC")),
            "data_liberacao_rc": _data(c.get("Data de liberação da RC")),
            "comprador_codigo": comp_cod,
            "comprador_nome": comp_nome,
            "planejador": _txt(c.get("Planejador")),
            "licitacao_numero": _txt(c.get("Número da licitação")),
            "licitacao_status": _txt(c.get("Status da licitação")),
            "licitacao_abertura": _data(c.get("Data de abertura da licitação")),
            "licitacao_encerramento": _data(c.get("Data de encerramento da licitação")),
            "raw": {"texto": bloco.strip(), "campos": c},
            "consultado_em": agora,
        }
    return list(itens.values())
