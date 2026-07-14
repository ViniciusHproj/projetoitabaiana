"""Helpers de formatação de data compartilhados entre as views.

Datas são guardadas no Mongo como string 'DD/MM/AAAA' (ou '—' se vazias),
enquanto os inputs HTML usam 'AAAA-MM-DD' — essas funções convertem entre
os dois formatos.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def formatar_data_br(data_str):
    """'AAAA-MM-DD' (input HTML) -> 'DD/MM/AAAA' (armazenado no Mongo).

    Usa strptime para validar — strings como '2026-01-15T00:00:00' que
    passariam num split simples são rejeitadas e retornam '—'.
    """
    if not data_str or data_str == '—':
        return '—'
    try:
        return datetime.strptime(data_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        logger.warning("formatar_data_br: formato inesperado recebido — %r", data_str)
        return '—'


def preparar_data_para_input(data_br):
    """'DD/MM/AAAA' (Mongo) -> 'AAAA-MM-DD' (input HTML)."""
    if not data_br or data_br == '—':
        return ""
    try:
        return datetime.strptime(data_br, "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        logger.warning("preparar_data_para_input: formato inesperado no Mongo — %r", data_br)
        return ""
