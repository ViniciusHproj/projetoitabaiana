"""Helpers de formatação de data compartilhados entre as views.

Datas são guardadas no Mongo como string 'DD/MM/AAAA' (ou '—' se vazias),
enquanto os inputs HTML usam 'AAAA-MM-DD' — essas funções convertem entre
os dois formatos.
"""


def formatar_data_br(data_str):
    """'AAAA-MM-DD' (input HTML) -> 'DD/MM/AAAA' (armazenado no Mongo)."""
    if not data_str or data_str == '—':
        return '—'
    try:
        ano, mes, dia = data_str.split('-')
        return f"{dia}/{mes}/{ano[:4]}"
    except Exception:
        return data_str


def preparar_data_para_input(data_br):
    """'DD/MM/AAAA' (Mongo) -> 'AAAA-MM-DD' (input HTML)."""
    if not data_br or data_br == '—':
        return ""
    try:
        dia, mes, ano = data_br.split('/')
        return f"{ano}-{mes}-{dia}"
    except Exception:
        return ""
