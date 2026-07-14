import os
import logging
import re
import threading
import time
import gspread
from google.oauth2.service_account import Credentials as GoogleServiceAccountCredentials
from django.shortcuts import render, redirect
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from django.contrib import messages
from django.core.cache import cache
import ntplib
import uuid
from datetime import datetime, timedelta, timezone
from django.contrib.auth.models import User

import cloudinary.uploader
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth import logout as auth_logout

from django.contrib.messages import get_messages
from django.utils.http import url_has_allowed_host_and_scheme
from django.http import HttpResponse, HttpResponseNotAllowed
from django.urls import reverse
from obras.utils import formatar_data_br, preparar_data_para_input
# Create your views here.

logger = logging.getLogger(__name__)


TENTATIVAS_BACKGROUND = 3
ESPERA_BACKGROUND_SEGUNDOS = (5, 15)  # espera antes da 2ª e da 3ª tentativa


def _disparar_em_background(funcao, *args, **kwargs):
    """
    Executa uma chamada de rede "melhor esforço" (ex: Google Sheets) numa thread
    separada, sem bloquear a resposta ao usuário. O Mongo já é a fonte de
    verdade, a planilha é só espelhamento — por isso falhas nunca sobem para o
    usuário. Tenta algumas vezes com espera crescente antes de desistir, pois
    falhas de rede/API do Google costumam ser passageiras; se todas as
    tentativas falharem, a falha é apenas logada (sem fila/retry persistente).
    """
    def _executar():
        for tentativa in range(1, TENTATIVAS_BACKGROUND + 1):
            try:
                funcao(*args, **kwargs)
                return
            except Exception:
                if tentativa < TENTATIVAS_BACKGROUND:
                    logger.warning(
                        "Tentativa %s/%s falhou para tarefa em segundo plano %s — tentando de novo em %ss.",
                        tentativa, TENTATIVAS_BACKGROUND, funcao.__name__,
                        ESPERA_BACKGROUND_SEGUNDOS[min(tentativa - 1, len(ESPERA_BACKGROUND_SEGUNDOS) - 1)], exc_info=True
                    )
                    # min() evita IndexError se TENTATIVAS_BACKGROUND for aumentado sem ampliar a tupla.
                    time.sleep(ESPERA_BACKGROUND_SEGUNDOS[min(tentativa - 1, len(ESPERA_BACKGROUND_SEGUNDOS) - 1)])
                else:
                    logger.exception(
                        "Erro ao executar tarefa em segundo plano após %s tentativas: %s",
                        TENTATIVAS_BACKGROUND, funcao.__name__
                    )

    threading.Thread(target=_executar, daemon=True).start()


CACHE_KEY_VERSAO_OBRAS = 'obras_cache_versao'


def _bump_cache_obras():
    """Invalida o cache de lista_obras após qualquer cadastro/edição de obra."""
    try:
        cache.incr(CACHE_KEY_VERSAO_OBRAS)
    except Exception:
        try:
            cache.set(CACHE_KEY_VERSAO_OBRAS, 1)
        except Exception:
            pass

# Cliente único reaproveitado entre requests (pymongo já faz pool de conexões internamente).
# connect=False adia a resolução de DNS/conexão real para o primeiro uso, em vez de travar
# a inicialização do Django caso o banco esteja temporariamente inacessível.
_mongo_client = MongoClient(os.environ['MONGODB_URI'], connect=False)
_db = _mongo_client[os.environ['MONGODB_DB_NAME']]
colecao_obras = _db['Banco_Obras']
colecao_funcionarios = _db['Banco_funcionarios']
colecao_timelapse = _db['Banco_Timelapse']
colecao_seguranca_login = _db['Banco_SegurancaLogin']
# Guarda, por usuário, qual é a sessão (session_key) considerada "ativa" no
# momento — atualizada a cada login bem-sucedido. Usada pelo
# SessaoUnicaMiddleware pra derrubar sessões antigas quando um login novo
# acontece em outro dispositivo/navegador (ver obras/middleware.py).
colecao_sessoes_ativas = _db['Banco_SessoesAtivas']

try:
    # Garante (de forma idempotente) que dois cadastros nunca gravem o mesmo ID_OBRA,
    # mesmo em corrida simultânea — ver retry em cadastro_obras.
    colecao_obras.create_index('ID_OBRA', unique=True)
except Exception:
    logger.exception("Não foi possível garantir o índice único em ID_OBRA")

try:
    # Acelera a ordenação por mais recente em lista_obras (sort + skip + limit).
    colecao_obras.create_index('TIMESTAMP_CADASTRO')
except Exception:
    logger.exception("Não foi possível garantir o índice em TIMESTAMP_CADASTRO")

try:
    # Acelera a busca/edição de funcionário por CPF.
    colecao_funcionarios.create_index('CPF')
except Exception:
    logger.exception("Não foi possível garantir o índice em CPF")

try:
    # Acelera a ordenação por mais recente na zona admin (sort por DATA_CADASTRO).
    colecao_funcionarios.create_index('DATA_CADASTRO')
except Exception:
    logger.exception("Não foi possível garantir o índice em DATA_CADASTRO (funcionários)")

try:
    # Acelera a busca de fotos/histórico de uma obra (galeria_obra, timelapse).
    colecao_timelapse.create_index('ID_OBRA')
except Exception:
    logger.exception("Não foi possível garantir o índice em ID_OBRA (timelapse)")

try:
    # TTL index: o Mongo apaga sozinho os documentos de bloqueio assim que
    # 'expira_em' é alcançado — não precisa de job de limpeza manual.
    colecao_seguranca_login.create_index('expira_em', expireAfterSeconds=0)
except Exception:
    logger.exception("Não foi possível garantir o índice TTL em Banco_SegurancaLogin")

try:
    # TTL index: remove registros de sessão ativa de usuários que não fazem
    # login há mais de 90 dias — mantém a coleção higienizada sem cleanup manual.
    colecao_sessoes_ativas.create_index('atualizado_em', expireAfterSeconds=90 * 24 * 3600)
except Exception:
    logger.exception("Não foi possível garantir o índice TTL em Banco_SessoesAtivas")

GOOGLE_SHEETS_CREDENTIALS_FILE = os.environ.get('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credenciais.json')
GOOGLE_SHEETS_SPREADSHEET_NAME = os.environ.get('GOOGLE_SHEETS_SPREADSHEET_NAME', 'Data base OBRAS DE ITABIANA')


# ==========================================
# RATE LIMIT DE LOGIN — persistido no MongoDB (não em cache de processo)
# ==========================================
# Usar LocMemCache para isso tem um problema em produção: é por processo, então
# um restart/deploy no Render (ou o app "dormindo" no free tier) zera todos os
# contadores de bloqueio de graça para qualquer atacante. Guardando no Mongo,
# o bloqueio sobrevive a restarts.
LOGIN_MAX_TENTATIVAS = 5
LOGIN_JANELA_BLOQUEIO_SEGUNDOS = 15 * 60  # 15 minutos
LOGIN_BLOQUEIOS_PARA_ALERTA = 3  # nº de bloqueios na mesma conta em 24h que dispara alerta de ataque direcionado
MAX_FOTOS_POR_ENVIO = 10  # limite de fotos por cadastro/edição de obra
MAX_TAMANHO_FOTO_BYTES = 10 * 1024 * 1024  # 10 MB por arquivo
_EXTENSOES_FOTO_PERMITIDAS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
# Magic bytes para cada formato permitido (offset 0, exceto PNG que tem 8 bytes)
_MAGIC_BYTES_FOTO = [
    (b'\xff\xd8\xff', 'JPEG'),
    (b'\x89PNG\r\n\x1a\n', 'PNG'),
    (b'GIF87a', 'GIF'),
    (b'GIF89a', 'GIF'),
    (b'RIFF', 'WEBP'),  # WEBP: RIFF....WEBP — checagem adicional abaixo
]
# Regex para extração de public_id Cloudinary (usadas em _extrair_public_id_cloudinary)
_RE_CLOUDINARY_VERSAO = re.compile(r'^v\d+$')
_RE_CLOUDINARY_TRANSFORM = re.compile(r'^[a-z]{1,3}_[a-zA-Z0-9]|,')
# Paginação e cache de lista_obras (pública) e zona_admin
_OBRAS_POR_PAGINA_PUBLICA = 12
_CACHE_TTL_LISTA_OBRAS = 120  # segundos
_OBRAS_POR_PAGINA_ADMIN = 10
# Projeção fixa para lista_obras — exclui GALERIA (array de até 10 URLs) e demais campos não exibidos
_CAMPOS_LISTA_OBRAS = {
    'ID_OBRA': 1, 'TIPO_OBRA': 1, 'VALOR_OBRA': 1, 'SITUACAO': 1,
    'EMPRESA_CONTRATADA': 1, 'TIPO_EXECUCAO': 1, 'ENDERECO': 1,
    'DATA_INICIO': 1, 'CONCLUSAO_PREVISTA': 1, 'DATA_FINALIZACAO': 1,
    'URL_FOTO': 1, '_id': 0,
}


def _validar_foto(foto):
    """Retorna None se a foto é válida, ou uma string de erro se não for."""
    ext = os.path.splitext(foto.name)[1].lower()
    if ext not in _EXTENSOES_FOTO_PERMITIDAS:
        return f'Tipo de arquivo "{ext}" não permitido. Use JPG, PNG, WEBP ou GIF.'
    if foto.size > MAX_TAMANHO_FOTO_BYTES:
        mb = foto.size / (1024 * 1024)
        return f'Arquivo "{foto.name}" muito grande ({mb:.1f} MB). Máximo 10 MB por foto.'
    cabecalho = foto.read(12)
    foto.seek(0)
    for magic, nome in _MAGIC_BYTES_FOTO:
        if cabecalho.startswith(magic):
            if nome == 'WEBP' and cabecalho[8:12] != b'WEBP':
                continue
            return None
    return f'O arquivo "{foto.name}" não é uma imagem válida.'


def _cargo_usuario(user):
    """Retorna 'GERENTE_GERAL', 'SUPERVISOR' ou 'COMUM' para um usuário autenticado."""
    if user.is_superuser:
        return 'GERENTE_GERAL'
    if user.is_staff:
        return 'SUPERVISOR'
    return 'COMUM'


def _ip_do_cliente(request):
    """Pega o IP real do request. Em produção (Render) o tráfego passa por um
    único proxy reverso confiável, que ACRESCENTA o IP real do cliente ao
    final de X-Forwarded-For antes de repassar a requisição — sem isso,
    REMOTE_ADDR seria sempre o IP do proxy, não do usuário, e o rate limit
    nunca bloquearia ninguém. Usamos o ÚLTIMO valor da lista (o mais próximo
    do servidor, escrito pelo proxy), nunca o primeiro: o primeiro valor é
    fornecido pelo próprio cliente e pode ser forjado livremente por um
    atacante para burlar o bloqueio por IP (cada tentativa com um
    X-Forwarded-For falso diferente cairia numa chave de rate-limit nova)."""
    encaminhado = request.META.get('HTTP_X_FORWARDED_FOR')
    if encaminhado:
        return encaminhado.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR', 'desconhecido')


def _formatar_tempo_restante(segundos):
    minutos = max(1, round(segundos / 60))
    return f"{minutos} minuto" if minutos == 1 else f"{minutos} minutos"


def _chaves_rate_limit_login(request, cpf):
    """Duas chaves: uma por IP (limita um único computador tentando várias
    contas) e outra por CPF (limita várias máquinas/IPs diferentes atacando
    a mesma conta — ex: ataque distribuído contra um único usuário)."""
    chave_ip = f'login_ip_{_ip_do_cliente(request)}'
    chave_cpf = f'login_cpf_{cpf}' if cpf else None
    return chave_ip, chave_cpf


def _ler_contador_login(chave):
    """Lê (contagem, segundos_restantes) do documento de rate-limit no Mongo."""
    doc = colecao_seguranca_login.find_one({'_id': chave})
    if not doc:
        return 0, 0
    # pymongo devolve datetimes sem tzinfo (naive UTC); comparar com naive UTC também.
    restante = (doc['expira_em'] - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    if restante <= 0:
        return 0, 0
    return doc.get('contagem', 0), restante


def _login_esta_bloqueado(chave_ip, chave_cpf):
    """Retorna (bloqueado, segundos_restantes). Quando ambas as chaves estão
    bloqueadas, usa o maior tempo restante para informar ao usuário."""
    maior_restante = 0

    contagem_ip, restante_ip = _ler_contador_login(chave_ip)
    if contagem_ip >= LOGIN_MAX_TENTATIVAS:
        maior_restante = max(maior_restante, restante_ip)

    if chave_cpf:
        contagem_cpf, restante_cpf = _ler_contador_login(chave_cpf)
        if contagem_cpf >= LOGIN_MAX_TENTATIVAS:
            maior_restante = max(maior_restante, restante_cpf)

    if maior_restante > 0:
        return True, maior_restante
    return False, 0


def _tentativas_restantes(chave_ip, chave_cpf):
    """Quantas tentativas ainda restam antes do bloqueio — usa a chave mais
    próxima do limite (a conta tentada ou o IP, o que estiver mais alto)."""
    usadas_ip, _ = _ler_contador_login(chave_ip)
    usadas_cpf, _ = _ler_contador_login(chave_cpf) if chave_cpf else (0, 0)
    usadas = max(usadas_ip, usadas_cpf)
    return max(0, LOGIN_MAX_TENTATIVAS - usadas)


def _incrementar_contador_login(chave, expira_em, agora):
    """Incrementa atomicamente o contador de tentativas e retorna a nova contagem."""
    resultado = colecao_seguranca_login.find_one_and_update(
        {'_id': chave},
        {'$inc': {'contagem': 1}, '$set': {'atualizado_em': agora}, '$setOnInsert': {'expira_em': expira_em}},
        upsert=True,
        return_document=True,
    )
    return resultado.get('contagem', 1) if resultado else 1


def _registrar_tentativa_falha_login(request, chave_ip, chave_cpf, cpf_tentado):
    """Registra tentativa falha e retorna (nova_contagem_ip, nova_contagem_cpf)
    para que o caller calcule tentativas restantes sem releitura do banco."""
    agora = datetime.now(timezone.utc)
    expira_em = agora + timedelta(seconds=LOGIN_JANELA_BLOQUEIO_SEGUNDOS)

    nova_contagem_ip = _incrementar_contador_login(chave_ip, expira_em, agora)
    nova_contagem_cpf = _incrementar_contador_login(chave_cpf, expira_em, agora) if chave_cpf else 0

    if max(nova_contagem_ip, nova_contagem_cpf) >= LOGIN_MAX_TENTATIVAS:
        logger.warning(
            "Login bloqueado por excesso de tentativas — IP=%s CPF=%s",
            _ip_do_cliente(request), cpf_tentado or '(vazio)'
        )
        if chave_cpf:
            _registrar_bloqueio_repetido(cpf_tentado)

    return nova_contagem_ip, nova_contagem_cpf


def _registrar_bloqueio_repetido(cpf_tentado):
    """Conta quantas vezes essa conta foi bloqueada nas últimas 24h. Vários
    bloqueios seguidos na mesma conta sugerem um ataque direcionado a um
    usuário específico (não só um erro de digitação ocasional)."""
    chave_alerta = f'login_alerta_cpf_{cpf_tentado}'
    agora = datetime.now(timezone.utc)
    expira_em = agora + timedelta(hours=24)

    # Incremento atômico: se o documento expirou (expira_em <= agora), reinicia o
    # contador em 1 e renova a expiração — tudo numa única operação sem race condition.
    resultado_alerta = colecao_seguranca_login.find_one_and_update(
        {'_id': chave_alerta},
        [{'$set': {
            'contagem': {'$cond': {
                'if': {'$or': [{'$not': ['$expira_em']}, {'$lte': ['$expira_em', agora]}]},
                'then': 1,
                'else': {'$add': [{'$ifNull': ['$contagem', 0]}, 1]},
            }},
            'expira_em': {'$cond': {
                'if': {'$or': [{'$not': ['$expira_em']}, {'$lte': ['$expira_em', agora]}]},
                'then': expira_em,
                'else': '$expira_em',
            }},
            'atualizado_em': agora,
        }}],
        upsert=True,
        return_document=True,
    )
    nova_contagem = resultado_alerta.get('contagem', 1) if resultado_alerta else 1

    if nova_contagem >= LOGIN_BLOQUEIOS_PARA_ALERTA:
        logger.warning(
            "ALERTA: a conta CPF=%s foi bloqueada %s vezes nas últimas 24h — "
            "possível ataque direcionado a este usuário específico.",
            cpf_tentado, nova_contagem
        )


def _limpar_tentativas_login(chave_ip, chave_cpf):
    colecao_seguranca_login.delete_one({'_id': chave_ip})
    if chave_cpf:
        colecao_seguranca_login.delete_one({'_id': chave_cpf})


def _form_ja_em_processamento(request, nome_acao):
    """Trava server-side contra duplo clique/duplo submit: o hx-indicator do
    HTMX só desabilita o botão visualmente — não impede um segundo POST real
    (ex: clique muito rápido, ou script automatizado). Usa o cache como lock
    de curta duração por usuário+ação. Só checa — não marca; use
    `_marcar_form_em_processamento` depois que a validação passar, para não
    travar o usuário por 'culpa' de um formulário que nem chegou a ser salvo."""
    identificador = request.user.pk if request.user.is_authenticated else _ip_do_cliente(request)
    chave = f'form_lock_{nome_acao}_{identificador}'
    return bool(cache.get(chave))


def _marcar_form_em_processamento(request, nome_acao, segundos=4):
    identificador = request.user.pk if request.user.is_authenticated else _ip_do_cliente(request)
    chave = f'form_lock_{nome_acao}_{identificador}'
    cache.set(chave, True, segundos)


def _gerar_form_token():
    return uuid.uuid4().hex


def _token_ja_usado(token):
    """Idempotência por token: cada carregamento do formulário recebe um
    token único (campo oculto). Se o mesmo token chegar duas vezes — duplo
    clique, segunda aba, replay de request — a segunda é rejeitada, mesmo
    que venha rápido o suficiente para escapar da trava de tempo acima.
    Token ausente é tratado como inválido: sem token não há idempotência."""
    if not token:
        return True  # POST sem token = envio inválido/forjado, rejeita
    chave = f'form_token_{token}'
    # cache.add é atômico: retorna False se a chave já existe, True se inseriu.
    # Evita race condition de dois POSTs simultâneos passando pelo get+set separado.
    return not cache.add(chave, True, 300)


def pagina_inicial(request):
    if request.headers.get('HX-Request'):
        return render(request, 'inicio_partial.html')
    return render(request, 'index.html')

def index(request):
    if request.headers.get('HX-Request'):
        return render(request, 'inicio_partial.html')
    return render(request, 'index.html')


def data_e_valida(data_str, tipo="geral"):
    """
    Verifica se a data existe e se está dentro de um limite histórico realista.
    O HTML envia a data no formato AAAA-MM-DD.
    """
    if not data_str or data_str == "—":
        return False
        
    try:
        data_obj = datetime.strptime(data_str, '%Y-%m-%d').date()
        ano_atual = datetime.now(timezone.utc).year

        # Regra 1: Absolutamente nenhuma data no sistema pode ser antes de 1900
        if data_obj.year < 1900:
            return False

        # Regra 2: Para funcionários (Não pode nascer no futuro e limite de 120 anos)
        if tipo == "nascimento":
            if data_obj > datetime.today().date() or (ano_atual - data_obj.year) > 120:
                return False
                
        # Regra 3: Para obras (Não devem ultrapassar um limite absurdo de prazo)
        elif tipo == "obra":
            if data_obj.year > 2100:
                return False

        return True
    except ValueError:
        # Cai aqui se a data for inválida (ex: 31/02/2026)
        return False


def validar_cpf(cpf):
    """Confere o dígito verificador do CPF (algoritmo oficial, módulo 11).
    Recebe só dígitos (sem pontuação). Rejeita também sequências repetidas
    (ex: 111.111.111-11), que passariam no cálculo mas não são CPFs reais."""
    if not cpf or len(cpf) != 11 or not cpf.isdigit() or cpf == cpf[0] * 11:
        return False

    for i in range(9, 11):
        soma = sum(int(cpf[num]) * ((i + 1) - num) for num in range(0, i))
        digito = ((soma * 10) % 11) % 10
        if digito != int(cpf[i]):
            return False
    return True


def validar_cnpj(cnpj):
    """Confere os dois dígitos verificadores do CNPJ (algoritmo oficial,
    módulo 11 com pesos). Recebe só dígitos (sem pontuação)."""
    if not cnpj or len(cnpj) != 14 or not cnpj.isdigit() or cnpj == cnpj[0] * 14:
        return False

    pesos_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]

    def _calcular_digito(base, pesos):
        soma = sum(int(d) * p for d, p in zip(base, pesos))
        resto = soma % 11
        return 0 if resto < 2 else 11 - resto

    digito_1 = _calcular_digito(cnpj[:12], pesos_1)
    digito_2 = _calcular_digito(cnpj[:12] + str(digito_1), pesos_2)
    return cnpj[-2:] == f"{digito_1}{digito_2}"


def validar_rg(rg):
    """RG não tem um algoritmo de dígito verificador padronizado entre
    estados (diferente de CPF/CNPJ) — aqui só confirmamos que, depois de
    limpo, sobrou algo plausível: só dígitos, com um tamanho razoável."""
    return bool(rg) and rg.isdigit() and 5 <= len(rg) <= 12


def _upload_com_retry(foto, pasta="obras_projeto", tentativas=3, espera=3):
    """Faz upload de um arquivo pro Cloudinary com até `tentativas` retries,
    esperando `espera` segundos entre cada um. Lança a última exceção se todas falharem."""
    ultima_exc = None
    for i in range(tentativas):
        try:
            return cloudinary.uploader.upload(foto, folder=pasta)
        except Exception as exc:
            ultima_exc = exc
            if i < tentativas - 1:
                time.sleep(espera)
    raise ultima_exc


def _valor_br_para_float(valor_str):
    """Converte string BR ('1.234,56') para float. Retorna 0.0 em caso de falha."""
    if not valor_str or valor_str == '—':
        return 0.0
    try:
        return float(str(valor_str).replace('.', '').replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0


def _float_para_br(valor_float):
    """Converte float para string BR ('1.234,56') para exibição e Google Sheets.
    Aceita também strings no formato BR como fallback defensivo, para cobrir
    documentos legados não migrados."""
    if not valor_float:
        return '0,00'
    try:
        v = float(valor_float)
    except (ValueError, TypeError):
        # Fallback: pode ser string BR ("1.234,56") ainda no banco
        try:
            v = float(str(valor_float).replace('.', '').replace(',', '.'))
        except (ValueError, TypeError):
            return '0,00'
    try:
        return f"{v:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    except (ValueError, TypeError):
        return '0,00'


def valor_e_valido(valor_str):
    """VALOR_OBRA chega do formulário já formatado em padrão BR (ex:
    '1.234,56' ou '1234,56'). Confere que, removendo a formatação, sobra um
    número positivo de verdade — não uma string qualquer."""
    if not valor_str:
        return False
    limpo = valor_str.replace('.', '').replace(',', '.')
    try:
        return float(limpo) > 0
    except ValueError:
        return False


def texto_tem_letra(texto):
    """Confere que o texto tem pelo menos uma letra — evita salvar campos
    como Nome/Endereço/Empresa preenchidos só com números ou símbolos."""
    return bool(texto) and any(c.isalpha() for c in texto)


SITUACOES_VALIDAS = {
    "Finalizada por conclusão de construção",
    "Finalizada por distrato",
    "Em andamento",
    "Paralisada",
    "Cancelada",
    "Em licitação",
}

TIPOS_EXECUCAO_VALIDOS = {
    "Nova Construção",
    "Reforma",
    "Ampliação",
    "Manutenção",
    "Restauração",
}


def login_view(request):
    # Só mostra o aviso de ?aviso= na carga inicial (GET). O <form> desta página
    # reenvia para request.get_full_path(), que inclui essa mesma query string —
    # se checássemos isso também no POST, a mensagem seria recriada a cada
    # tentativa de login e duplicaria com a de sucesso/erro do POST.
    if request.method == 'GET':
        if request.user.is_authenticated:
            return redirect('inicio')
        aviso = request.GET.get('aviso', '')
        if aviso == 'saiu':
            messages.info(request, "Sessão encerrada com sucesso.")
            return redirect('login')
        aviso_sessao = request.session.pop('aviso_login', '')
        return render(request, 'login.html', {'aviso_login': aviso_sessao})

    if request.method == 'POST':
        if request.user.is_authenticated:
            return redirect('inicio')
        usuario_cpf = request.POST.get('username', '').replace('.', '').replace('-', '')
        senha_digitada = request.POST.get('password', '')

        chave_ip, chave_cpf = _chaves_rate_limit_login(request, usuario_cpf)
        bloqueado, segundos_restantes = _login_esta_bloqueado(chave_ip, chave_cpf)
        if bloqueado:
            tempo = _formatar_tempo_restante(segundos_restantes)
            messages.error(request, f"Muitas tentativas de login incorretas. Tente novamente em {tempo}.")
            # Redirect (não render direto) — assim um F5 do usuário não reenvia o
            # POST e não gasta tentativa/bloqueio de novo à toa.
            return redirect('login')

        user = authenticate(request, username=usuario_cpf, password=senha_digitada)

        if user is not None:
            # 1. Faz o login
            auth_login(request, user)
            _limpar_tentativas_login(chave_ip, chave_cpf)

            # 2. Registra esta sessão como a "ativa" do usuário — qualquer
            # outra sessão dele (outro dispositivo/navegador já logado) será
            # derrubada na próxima requisição que fizer (ver SessaoUnicaMiddleware).
            if not request.session.session_key:
                request.session.save()
            colecao_sessoes_ativas.update_one(
                {'_id': user.pk},
                {'$set': {'session_key': request.session.session_key, 'atualizado_em': datetime.now(timezone.utc)}},
                upsert=True,
            )

            # Descarta qualquer mensagem pendente (ex: aviso de sessão expirada
            # que foi gerado antes do login e não deve aparecer na tela de boas-vindas).
            storage = messages.get_messages(request)
            storage.used = True

            partes_nome = (user.first_name or "").split()
            nome_usuario = partes_nome[0].title() if partes_nome else user.username
            if user.is_superuser:
                funcao = "Gerente Geral"
            elif user.is_staff:
                funcao = "Supervisor"
            else:
                funcao = "Funcionário Comum"

            proxima_raw = request.GET.get('next', '')
            if proxima_raw and url_has_allowed_host_and_scheme(
                proxima_raw,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                proxima_url = proxima_raw
            else:
                proxima_url = reverse('inicio')

            # Renderiza a própria página de login com o modal de sucesso visível.
            # O botão "Continuar" do modal é um <a href> que redireciona para proxima_url
            # sem JavaScript inline — o usuário confirma antes de seguir.
            return render(request, 'login.html', {
                'login_success': True,
                'nome_usuario': nome_usuario,
                'funcao': funcao,
                'proxima_url': proxima_url,
            })
        else:
            nova_c_ip, nova_c_cpf = _registrar_tentativa_falha_login(request, chave_ip, chave_cpf, usuario_cpf)
            usadas = max(nova_c_ip, nova_c_cpf)
            restantes = max(0, LOGIN_MAX_TENTATIVAS - usadas)
            # Se errar o login, também limpamos antes de mostrar o erro
            storage = get_messages(request)
            for _ in storage: pass

            if restantes > 0:
                plural = "tentativa" if restantes == 1 else "tentativas"
                messages.error(request, f"CPF ou Senha incorretos. Você tem mais {restantes} {plural} antes do bloqueio temporário.")
            else:
                tempo = _formatar_tempo_restante(LOGIN_JANELA_BLOQUEIO_SEGUNDOS)
                messages.error(request, f"CPF ou Senha incorretos. Login bloqueado por {tempo} devido ao excesso de tentativas.")
            # Redirect (não render direto) — mesmo motivo: evitar reenvio da
            # tentativa de login (e do POST de senha) num F5.
            return redirect('login')

    return HttpResponseNotAllowed(['GET', 'POST'])


def logout_view(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    motivo = request.POST.get('motivo', '')
    if request.user.is_authenticated:
        try:
            colecao_sessoes_ativas.delete_one({'_id': request.user.pk})
        except Exception:
            logger.exception("Erro ao remover sessão ativa no logout — user=%s", request.user.pk)
        auth_logout(request)
    if motivo == 'inatividade':
        request.session['aviso_login'] = 'inatividade'
        return redirect('login')
    return redirect('/login/?aviso=saiu')

def pegar_ano_google():
    # Resultado cacheado por 1h — evita bloquear o thread do request numa chamada UDP
    # ao servidor NTP a cada cadastro de obra. O ano muda uma vez por ano; 1h é mais
    # que suficiente para capturar a virada sem nunca atrasar uma resposta HTTP.
    _CACHE_KEY_ANO = 'ano_ntp'
    ano = cache.get(_CACHE_KEY_ANO)
    if ano is not None:
        return ano
    try:
        cliente_ntp = ntplib.NTPClient()
        resposta = cliente_ntp.request('pool.ntp.org', version=3)
        ano = datetime.utcfromtimestamp(resposta.tx_time).year
    except Exception:
        ano = datetime.now(timezone.utc).year
    cache.set(_CACHE_KEY_ANO, ano, 3600)
    return ano


def cadastro_funcionario(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    # 2. Bloqueio para quem está logado mas é COMUM (não é staff)
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Acesso negado. Esta área é exclusiva para supervisores.")
        return redirect('inicio')
        
    def _render_cadastro_func(msg, nivel='warning'):
        getattr(messages, nivel)(request, msg)
        # Guarda os campos digitados na sessão (não dá pra repor depois de um
        # redirect de outra forma) e redireciona — em vez de devolver a página
        # direto do POST, o que faria o navegador reenviar o formulário a cada F5.
        # SENHA é excluída: nunca deve ficar em plaintext na sessão (MongoDB).
        dados = request.POST.dict()
        dados.pop('SENHA', None)
        request.session['erro_cadastro_funcionario'] = dados
        return redirect('cadastro_funcionario')

    if request.method == 'POST' and 'btn-salvar' in request.POST:
        if _form_ja_em_processamento(request, 'cadastro_funcionario'):
            return _render_cadastro_func("Já recebemos uma solicitação recente. Evite enviar mais de uma vez.")

        if _token_ja_usado(request.POST.get('form_token', '')):
            return _render_cadastro_func("Este cadastro já foi enviado. Se precisar cadastrar outro funcionário, recarregue a página.")  # consome o token sem rejeitar

        try:
            rg_limpo = request.POST.get('RG', '').replace('.', '').replace('-', '')
            cpf_limpo = request.POST.get('CPF', '').replace('.', '').replace('-', '')
            
            # 1. COLETA OS DADOS CRUS E PROTEGIDOS
            documento_funcionario = {
                'NOME': request.POST.get('NOME', '').upper(),
                'DATA_NASCIMENTO': request.POST.get('DATA_NASCIMENTO', ''),
                'RG': rg_limpo,
                'CPF': cpf_limpo,
                'SENHA': request.POST.get('SENHA', ''),
                'NIVEL_ACESSO': request.POST.get('NIVEL_ACESSO', '').upper()
            }

            # 2. VALIDAÇÃO EXPLÍCITA (Campos Vazios)
            campos_obrigatorios = ['NOME', 'DATA_NASCIMENTO', 'RG', 'CPF', 'SENHA', 'NIVEL_ACESSO']

            for campo in campos_obrigatorios:
                valor = documento_funcionario.get(campo, '')
                if not valor or str(valor).strip() == "" or valor == '—':
                    return _render_cadastro_func("Preencha todos os campos obrigatórios.")

            # ==========================================
            # 2.5 TRAVA DE SEGURANÇA DA DATA DE NASCIMENTO
            # ==========================================
            data_nasc_str = documento_funcionario['DATA_NASCIMENTO']
            
            # Chama a função validadora que criamos no topo do views.py
            if not data_e_valida(data_nasc_str, tipo="nascimento"):
                return _render_cadastro_func("A data de nascimento informada é inválida ou irreal.")

            # Verifica idade mínima: 18 anos para Supervisor, 16 para Comum.
            _nasc = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()
            _hoje = datetime.today().date()
            _idade = _hoje.year - _nasc.year - ((_hoje.month, _hoje.day) < (_nasc.month, _nasc.day))
            _nivel = documento_funcionario.get('NIVEL_ACESSO', '')
            if _nivel == 'SUPERVISOR' and _idade < 18:
                return _render_cadastro_func("Supervisores devem ter no mínimo 18 anos.")
            if _nivel == 'COMUM' and _idade < 16:
                return _render_cadastro_func("Funcionários devem ter no mínimo 16 anos.")

            # Validação do nível de acesso — deve ser um dos dois valores aceitos.
            # Sem isso, um valor arbitrário como "GERENTE" passaria pela validação,
            # seria salvo como FUNCAO no MongoDB e o usuário ficaria como COMUM no Django.
            cargo_editor = _cargo_usuario(request.user)
            NIVEIS_VALIDOS = {'SUPERVISOR', 'COMUM'} if cargo_editor == 'GERENTE_GERAL' else {'COMUM'}
            if _nivel not in NIVEIS_VALIDOS:
                if cargo_editor == 'SUPERVISOR':
                    return _render_cadastro_func("Supervisores só podem cadastrar funcionários comuns.")
                return _render_cadastro_func("O nível de acesso selecionado é inválido.")
            # ==========================================

            # ==========================================
            # 2.6 TRAVA DE SEGURANÇA DO CPF (dígito verificador)
            # ==========================================
            if not validar_cpf(documento_funcionario['CPF']):
                return _render_cadastro_func("O CPF informado é inválido.")
            # ==========================================

            # ==========================================
            # 2.7 TRAVA DE SEGURANÇA DO RG E DO NOME
            # ==========================================
            if not validar_rg(documento_funcionario['RG']):
                return _render_cadastro_func("O RG informado é inválido (deve conter apenas números).")

            if not texto_tem_letra(documento_funcionario['NOME']):
                return _render_cadastro_func("O Nome informado é inválido.")
            # ==========================================

            # ==========================================
            # 2.8 VALIDAÇÃO DE SENHA
            # ==========================================
            senha_raw = documento_funcionario['SENHA']
            if len(senha_raw) < 8:
                return _render_cadastro_func("A senha deve ter pelo menos 8 caracteres.")
            # ==========================================

            # 3. VERIFICAÇÃO DE DUPLICIDADE (Evita crash no Django)
            cpf = documento_funcionario['CPF']
            if User.objects.filter(username=cpf).exists():
                return _render_cadastro_func('Já existe um funcionário cadastrado com este CPF.', nivel='error')

            # Só trava contra duplo submit agora que passamos por todas as validações —
            # assim um erro de validação não consome a janela de bloqueio à toa.
            _marcar_form_em_processamento(request, 'cadastro_funcionario')

            # 4. CÁLCULO DA IDADE (Agora é 100% seguro, pois a data foi validada acima)
            data_nascimento_obj = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()
            hoje = datetime.today().date()
            idade = hoje.year - data_nascimento_obj.year - ((hoje.month, hoje.day) < (data_nascimento_obj.month, data_nascimento_obj.day))

            # 5. CRIAÇÃO DO USUÁRIO NO DJANGO (Acesso ao sistema)
            senha = documento_funcionario['SENHA']
            nome = documento_funcionario['NOME']
            nivel = documento_funcionario['NIVEL_ACESSO']

            novo_usuario = User.objects.create_user(username=cpf, password=senha, first_name=nome)
            if nivel == 'SUPERVISOR':
                novo_usuario.is_staff = True
                novo_usuario.save()

            # 6. FORMATAÇÃO E AJUSTES PARA O MONGODB
            documento_funcionario['DATA_NASCIMENTO'] = formatar_data_br(data_nasc_str)
            documento_funcionario['IDADE'] = idade
            documento_funcionario['FUNCAO'] = nivel
            documento_funcionario['DATA_CADASTRO'] = datetime.now(timezone.utc)
            
            # Removemos a senha e o NIVEL_ACESSO temporário antes de enviar para o MongoDB
            del documento_funcionario['SENHA']
            del documento_funcionario['NIVEL_ACESSO']

            # 7. INSERÇÃO NO MONGODB
            # Se isso falhar, o usuário Django já foi criado (passo 5) e ficaria
            # "órfão" — consegue logar mas sem perfil em Banco_funcionarios.
            # Como as duas gravações não compartilham uma transação (bancos
            # diferentes), desfazemos manualmente o usuário Django nesse caso.
            try:
                colecao_funcionarios.insert_one(documento_funcionario)
            except Exception:
                novo_usuario.delete()
                raise

            messages.success(request, f"Funcionário {nome} cadastrado com sucesso.")
            return redirect('cadastro_funcionario')

        except Exception:
            logger.exception("Erro ao cadastrar funcionário")
            return _render_cadastro_func("Erro inesperado ao cadastrar. Tente novamente.", nivel='error')

    contexto_token = {'form_token': _gerar_form_token(), 'cargo': _cargo_usuario(request.user)}
    dados_repostos = request.session.pop('erro_cadastro_funcionario', None)
    if dados_repostos:
        contexto_token['dados'] = dados_repostos

    if request.headers.get('HX-Request'):
        return render(request, 'cadastro_funcionario.html', contexto_token)

    return render(request, 'index.html', {'template_meio': 'cadastro_funcionario.html', **contexto_token})



def busca_atualiza_funcionario(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    # 2. Bloqueio para quem está logado mas é COMUM (não é staff)
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Acesso negado. Esta área é exclusiva para supervisores.")
        return redirect('inicio')
    # Lógica para o POST (Quando o usuário clica em "Buscar")
    if request.method == 'POST':
        cpf_pesquisado = request.POST.get('CPF', '').strip()
        cpf_limpo = cpf_pesquisado.replace('.', '').replace('-', '')

        if not cpf_limpo or not cpf_limpo.isdigit():
            messages.error(request, "Informe um CPF válido para buscar.")
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_funcionario.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})

        funcionario = colecao_funcionarios.find_one({'CPF': cpf_limpo})

        if funcionario:
            # --- CONVERSÃO DA DATA (BR para HTML) ---
            data_nasc = funcionario.get('DATA_NASCIMENTO', '')
            if data_nasc and '/' in data_nasc:
                d, m, a = data_nasc.split('/')
                funcionario['DATA_NASCIMENTO'] = f"{a}-{m}-{d}"

            # Registra qual CPF está autorizado a ser editado nesta sessão —
            # salva_edicao_funcionario verifica este valor para impedir que o
            # formulário seja forjado para editar um CPF diferente do buscado.
            request.session['cpf_editando'] = cpf_limpo

            contexto = {'funcionario': funcionario, 'form_token': _gerar_form_token(), 'cargo': _cargo_usuario(request.user)}

            # Se for HTMX, manda só o formulário de edição
            if request.headers.get('HX-Request'):
                return render(request, 'edita_funcionario.html', contexto)

            # Se for acesso direto (F5 no resultado da busca), manda a index com o template de edição
            return render(request, 'index.html', {
                'template_meio': 'edita_funcionario.html',
                **contexto
            })

        else:
            messages.error(request, "Funcionário não encontrado.")
            # Se for HTMX, volta para a tela de busca
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_funcionario.html')
            
            # Se for acesso direto, recarrega a index com a busca
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})

    # --- Lógica para o GET (Quando o usuário apenas abre a URL) ---
    
    # Se for clique no menu (HTMX)
    if request.headers.get('HX-Request'):
        return render(request, 'busca_atualiza_funcionario.html')
    # Se for acesso direto pela URL ou F5
    return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})


def salva_edicao_funcionario(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    # 2. Bloqueio para quem está logado mas é COMUM (não é staff)
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Acesso negado. Esta área é exclusiva para supervisores.")
        return redirect('inicio')

    def _dados_para_repor(request):
        """Snapshot dos campos digitados, pra reexibir o formulário de edição
        preenchido depois do redirect (em vez de perder tudo e mandar o
        usuário refazer a busca)."""
        return {
            'CPF': request.POST.get('CPF_ORIGINAL', ''),
            'NOME': request.POST.get('NOME', ''),
            'DATA_NASCIMENTO': request.POST.get('DATA_NASCIMENTO', ''),
            'RG': request.POST.get('RG', ''),
            'FUNCAO': request.POST.get('FUNCAO', ''),
        }

    if request.method == 'POST':
        if _form_ja_em_processamento(request, 'salva_edicao_funcionario'):
            messages.warning(request, "Já recebemos uma solicitação recente. Evite enviar mais de uma vez.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        if _token_ja_usado(request.POST.get('form_token', '')):
            messages.warning(request, "Esta edição já foi enviada. Se precisar editar novamente, refaça a busca.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        cpf_original = request.POST.get('CPF_ORIGINAL', '').strip()

        # Verifica que o CPF sendo editado é o mesmo que foi buscado nesta sessão —
        # impede que um supervisor forje o campo oculto para editar/tomar conta de
        # outro funcionário sem ter passado pela tela de busca daquele CPF.
        cpf_autorizado = request.session.get('cpf_editando', '')
        if not cpf_original or cpf_original != cpf_autorizado:
            messages.error(request, "Sessão de edição inválida. Refaça a busca do funcionário.")
            return redirect('busca_atualiza_funcionario')

        # Supervisor não pode editar outro supervisor ou gerente geral.
        # Gerente Geral não pode editar outro Gerente Geral.
        cargo_editor = _cargo_usuario(request.user)
        try:
            usuario_alvo_check = User.objects.get(username=cpf_autorizado)
            if cargo_editor == 'SUPERVISOR' and (usuario_alvo_check.is_staff or usuario_alvo_check.is_superuser):
                messages.error(request, "Supervisores não podem editar outros supervisores ou o Gerente Geral.")
                return redirect('zona_admin')
            if cargo_editor == 'GERENTE_GERAL' and usuario_alvo_check.is_superuser:
                messages.error(request, "Não é possível editar outro Gerente Geral.")
                return redirect('zona_admin')
        except User.DoesNotExist:
            pass

        # 1. COLETA E LIMPEZA INICIAL DOS DADOS
        nome_novo = request.POST.get('NOME', '').strip()
        funcao_nova = request.POST.get('FUNCAO', '').strip()
        rg_novo = request.POST.get('RG', '').strip()
        data_input = request.POST.get('DATA_NASCIMENTO', '').strip()

        # ==========================================
        # 2. TRAVA DE SEGURANÇA CONTRA CAMPOS VAZIOS
        # ==========================================
        campos_obrigatorios = [nome_novo, funcao_nova, rg_novo, data_input]

        if any(not campo for campo in campos_obrigatorios):
            messages.warning(request, "Não é permitido deixar Nome, RG, Função ou Data de Nascimento em branco.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')
        # ==========================================

        # ==========================================
        # 2.5 TRAVA DE SEGURANÇA DA DATA DE NASCIMENTO
        # ==========================================
        if not data_e_valida(data_input, tipo="nascimento"):
            messages.warning(request, "A data de nascimento informada é inválida ou irreal.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        # Verifica idade mínima: 18 anos para Supervisor, 16 para Comum.
        _nasc = datetime.strptime(data_input, '%Y-%m-%d').date()
        _hoje = datetime.today().date()
        _idade = _hoje.year - _nasc.year - ((_hoje.month, _hoje.day) < (_nasc.month, _nasc.day))
        if funcao_nova.upper() == 'SUPERVISOR' and _idade < 18:
            messages.warning(request, "Supervisores devem ter no mínimo 18 anos.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')
        if funcao_nova.upper() == 'COMUM' and _idade < 16:
            messages.warning(request, "Funcionários devem ter no mínimo 16 anos.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')
        # ==========================================

        # ==========================================
        # 2.6 TRAVA DE SEGURANÇA DO RG E DO NOME
        # ==========================================
        if not validar_rg(rg_novo.replace('.', '').replace('-', '')):
            messages.warning(request, "O RG informado é inválido (deve conter apenas números).")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        if not texto_tem_letra(nome_novo):
            messages.warning(request, "O Nome informado é inválido.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')
        # ==========================================

        # ==========================================
        # 2.7 TRAVA DE SEGURANÇA DA FUNÇÃO
        # ==========================================
        FUNCOES_VALIDAS = {'SUPERVISOR', 'COMUM'} if _cargo_usuario(request.user) == 'GERENTE_GERAL' else {'COMUM'}
        if funcao_nova.upper() not in FUNCOES_VALIDAS:
            messages.warning(request, "A função selecionada é inválida ou não permitida para o seu cargo.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')
        funcao_nova = funcao_nova.upper()
        # ==========================================

        # ==========================================
        # 2.8 VALIDAÇÃO DE SENHA
        # ==========================================
        nova_senha_digitada = request.POST.get('SENHA', '')
        if nova_senha_digitada.strip():
            if len(nova_senha_digitada.strip()) < 8:
                messages.warning(request, "A senha deve ter pelo menos 8 caracteres.")
                request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
                return redirect('salva_edicao_funcionario')
        # ==========================================

        # Só trava contra duplo submit agora que passamos por todas as validações.
        _marcar_form_em_processamento(request, 'salva_edicao_funcionario')

        # 3. Formata a data para o padrão BR (DD/MM/AAAA)
        data_br = formatar_data_br(data_input)

        # 4. Prepara os dados para o MongoDB
        nome_novo = nome_novo.upper()
        rg_limpo = rg_novo.replace('.', '').replace('-', '')

        # Recalcula IDADE a partir da nova data de nascimento para manter consistência.
        data_nasc_obj = datetime.strptime(data_input, '%Y-%m-%d').date()
        hoje = datetime.today().date()
        idade_nova = hoje.year - data_nasc_obj.year - ((hoje.month, hoje.day) < (data_nasc_obj.month, data_nasc_obj.day))

        dados_atualizados_mongo = {
            'NOME': nome_novo,
            'DATA_NASCIMENTO': data_br,
            'RG': rg_limpo,
            'FUNCAO': funcao_nova,
            'IDADE': idade_nova,
        }

        # 5. ATUALIZAÇÃO NA TABELA DE AUTENTICAÇÃO (DJANGO)
        try:
            usuario_django = User.objects.get(username=cpf_original)
        except User.DoesNotExist:
            messages.error(request, "Usuário não encontrado na tabela de autenticação.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        # Guarda estado original para rollback caso o MongoDB falhe depois.
        _nome_original = usuario_django.first_name
        _staff_original = usuario_django.is_staff
        _password_original = usuario_django.password

        usuario_django.first_name = nome_novo
        usuario_django.is_staff = (funcao_nova == 'SUPERVISOR')

        # Reutiliza a senha já validada acima — evita re-leitura do POST.
        if nova_senha_digitada.strip():
            usuario_django.set_password(nova_senha_digitada.strip())

        usuario_django.save()

        # 6. ATUALIZAÇÃO NO MONGODB
        # Se falhar, reverte o Django para evitar que os dois bancos fiquem
        # em estados divergentes permanentemente.
        try:
            resultado_mongo = colecao_funcionarios.update_one({'CPF': cpf_original}, {'$set': dados_atualizados_mongo})
        except Exception:
            try:
                usuario_django.first_name = _nome_original
                usuario_django.is_staff = _staff_original
                usuario_django.password = _password_original
                usuario_django.save(update_fields=['first_name', 'is_staff', 'password'])
            except Exception:
                logger.exception(
                    "CRÍTICO: falha ao fazer rollback do Django após erro no MongoDB "
                    "— estados divergentes para CPF=%s", cpf_original
                )
            logger.exception("Erro ao salvar edição de funcionário CPF=%s no MongoDB", cpf_original)
            messages.error(request, "Erro ao salvar no banco de dados. Tente novamente.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        if resultado_mongo.matched_count == 0:
            # Documento não encontrado no Mongo — reverte Django para evitar divergência.
            try:
                usuario_django.first_name = _nome_original
                usuario_django.is_staff = _staff_original
                usuario_django.password = _password_original
                usuario_django.save(update_fields=['first_name', 'is_staff', 'password'])
            except Exception:
                logger.exception(
                    "CRÍTICO: falha ao fazer rollback do Django após matched_count=0 no MongoDB "
                    "— estados divergentes para CPF=%s", cpf_original
                )
            messages.error(
                request,
                f"Perfil do funcionário {cpf_original} não encontrado no banco de dados. "
                "Contate o administrador do sistema."
            )
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        messages.success(request, f"Dados de {nome_novo} atualizados com sucesso.")
        # Redirect (PRG) — sem isso, um F5 depois de salvar reenviaria o POST
        # e tentaria salvar a edição de novo.
        return redirect('salva_edicao_funcionario')

    # Retorno adequado para HTMX ou acesso direto (GET, ou após o redirect acima)
    dados_repostos = request.session.pop('erro_edicao_funcionario', None)
    if dados_repostos:
        contexto_erro = {'funcionario': dados_repostos, 'form_token': _gerar_form_token(), 'cargo': _cargo_usuario(request.user)}
        if request.headers.get('HX-Request'):
            return render(request, 'edita_funcionario.html', contexto_erro)
        return render(request, 'index.html', {'template_meio': 'edita_funcionario.html', **contexto_erro})

    if request.headers.get('HX-Request'):
        return render(request, 'busca_atualiza_funcionario.html')
    return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})


# --- SUA VIEW DE CADASTRO DE OBRA ---


def cadastro_obras(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    if request.method == 'POST' and 'btn-salvar' in request.POST:
        def _render_cadastro(msg, nivel='warning'):
            getattr(messages, nivel)(request, msg)
            # Guarda os campos (texto) na sessão e redireciona — evita que um F5
            # reenvie o POST (e as fotos) de novo. As fotos em si não dá pra repor
            # (não vêm de volta do navegador), o usuário precisa reanexá-las.
            request.session['erro_cadastro_obras'] = request.POST.dict()
            return redirect('Cadastro-Obras')

        if _form_ja_em_processamento(request, 'cadastro_obras'):
            return _render_cadastro("Já recebemos uma solicitação recente. Evite enviar mais de uma vez.")

        if _token_ja_usado(request.POST.get('form_token', '')):
            return _render_cadastro("Este cadastro já foi enviado. Se precisar cadastrar outra obra, recarregue a página.")

        # Coleta fora do try para que erros de validação nunca sejam engolidos pelo except externo
        id_obra_manual = request.POST.get('ID_OBRA_MANUAL', '').strip()
        tipo_obra = request.POST.get('TIPO_OBRA', '').strip().upper()
        situacao = request.POST.get('SITUACAO', '').strip()
        tipo_execucao = request.POST.get('TIPO_EXECUCAO', '').strip()
        valor_obra = request.POST.get('VALOR_OBRA', '').strip()
        data_inicio = request.POST.get('DATA_INICIO', '').strip()
        conclusao_prevista = request.POST.get('CONCLUSAO_PREVISTA', '').strip()
        data_finalizacao = request.POST.get('DATA_FINALIZACAO', '').strip()
        nome_empresa = request.POST.get('NOME_EMPRESA', '').strip()
        endereco = request.POST.get('ENDERECO', '').strip()
        cnpj_empresa = request.POST.get('CNPJ_EMPRESA', '').strip()
        fotos_arquivos = request.FILES.getlist('FOTO_OBRA')

        # ==========================================
        # VALIDAÇÕES (fora do try — nunca engolidas pelo except)
        # ==========================================
        campos_obrigatorios = [
            tipo_obra, situacao, valor_obra, data_inicio,
            conclusao_prevista, nome_empresa, cnpj_empresa,
            tipo_execucao, endereco
        ]

        if any(not campo for campo in campos_obrigatorios) or not fotos_arquivos:
            return _render_cadastro("Preencha todos os campos obrigatórios e envie pelo menos uma foto da obra.")

        if len(fotos_arquivos) > MAX_FOTOS_POR_ENVIO:
            return _render_cadastro(f"Envie no máximo {MAX_FOTOS_POR_ENVIO} fotos por cadastro.")

        for foto in fotos_arquivos:
            erro_foto = _validar_foto(foto)
            if erro_foto:
                return _render_cadastro(erro_foto)

        if not validar_cnpj(cnpj_empresa.replace('.', '').replace('/', '').replace('-', '')):
            return _render_cadastro("O CNPJ informado é inválido.")

        if not valor_e_valido(valor_obra):
            return _render_cadastro("O Valor Total da Obra informado é inválido.")

        if situacao not in SITUACOES_VALIDAS:
            return _render_cadastro("A Situação selecionada é inválida.")

        if tipo_execucao not in TIPOS_EXECUCAO_VALIDOS:
            return _render_cadastro("O Tipo de Execução selecionado é inválido.")

        if id_obra_manual and not id_obra_manual.isdigit():
            return _render_cadastro("O ID da Obra (quando informado manualmente) deve conter apenas números.")

        if not texto_tem_letra(tipo_obra) or not texto_tem_letra(nome_empresa) or not texto_tem_letra(endereco):
            return _render_cadastro("Tipo de Obra, Nome da Empresa e Endereço não podem conter apenas números ou símbolos.")

        if not data_e_valida(data_inicio, tipo="obra") or not data_e_valida(conclusao_prevista, tipo="obra"):
            return _render_cadastro("A Data de Início ou a Conclusão Prevista contém um ano inválido ou irreal.")

        if data_finalizacao and not data_e_valida(data_finalizacao, tipo="obra"):
            return _render_cadastro("A Data de Finalização informada é inválida ou irreal.")

        dt_inicio = datetime.strptime(data_inicio, '%Y-%m-%d').date()
        dt_conclusao = datetime.strptime(conclusao_prevista, '%Y-%m-%d').date()

        if dt_inicio >= dt_conclusao:
            return _render_cadastro("A Conclusão Prevista deve ser posterior à Data de Início.")

        if data_finalizacao:
            dt_finalizacao = datetime.strptime(data_finalizacao, '%Y-%m-%d').date()
            if dt_inicio >= dt_finalizacao:
                return _render_cadastro("A Data de Finalização deve ser posterior à Data de Início.")

        # Só trava contra duplo submit agora que a validação passou.
        _marcar_form_em_processamento(request, 'cadastro_obras', segundos=12)

        try:
            # 4. UPLOAD MÚLTIPLO (Cloudinary)
            colecao = colecao_obras
            urls_galeria = []
            public_ids_enviados = []
            try:
                for foto in fotos_arquivos:
                    resultado_upload = _upload_com_retry(foto)
                    public_ids_enviados.append(resultado_upload.get('public_id', ''))
                    urls_galeria.append(resultado_upload.get('secure_url'))
            except Exception:
                # Limpa uploads já realizados antes de abortar — evita órfãos no Cloudinary.
                for pid in public_ids_enviados:
                    if pid:
                        try:
                            cloudinary.uploader.destroy(pid)
                        except Exception:
                            pass
                return _render_cadastro("Erro ao enviar as imagens para o servidor.", nivel='error')

            url_da_foto_capa = urls_galeria[0] if urls_galeria else ""

            # 5. MONTAGEM DO DICIONÁRIO (Fiel ao Banco e Planilha)

            # Padronização da Empresa para a Planilha
            empresa_completa = f"{nome_empresa.upper()} - CNPJ - {cnpj_empresa}"
            agora = datetime.now(timezone.utc)
            ano_atual = pegar_ano_google()

            # ==========================================
            # 6. GERAÇÃO DE ID + SALVAMENTO ATÔMICO
            # ==========================================
            # O número usado é o maior prefixo numérico existente para o ano + 1.
            # Usar max em vez de count garante que deleções não-finais não colidam:
            # se obras 1-3 existem e obra 2 é deletada, count=2 tentaria "32026" (já
            # existe), mas max=3 gera "42026" corretamente. Índice único em ID_OBRA
            # rejeita colisão de dois requests simultâneos; o retry recalcula max e
            # tenta o próximo número automaticamente.
            resultado = None
            id_obra_gerado = id_obra_manual or None

            sufixo_len = len(str(ano_atual))
            for _tentativa in range(10):
                if not id_obra_manual:
                    # Usa o maior prefixo numérico do ano (não count) para tolerar deleções
                    # não-finais: se obras 1-3 existem e obra 2 é deletada, count=2 geraria
                    # "32026" que já existe; max=3 gera "42026" corretamente.
                    res_max = list(colecao.aggregate([
                        {"$match": {"ID_OBRA": {"$regex": f"^\\d+{ano_atual}$"}}},
                        {"$project": {"num": {"$toInt": {"$substr": [
                            "$ID_OBRA", 0,
                            {"$subtract": [{"$strLenCP": "$ID_OBRA"}, sufixo_len]}
                        ]}}}},
                        {"$group": {"_id": None, "max_num": {"$max": "$num"}}},
                    ]))
                    proximo_num = (res_max[0]["max_num"] + 1) if res_max else 1
                    id_obra_gerado = f"{proximo_num}{ano_atual}"

                nova_obra = {
                    'ID_OBRA': id_obra_gerado,
                    'TIPO_OBRA': tipo_obra,      # Ex: PAVIMENTAÇÃO (Maiúsculo)
                    'SITUACAO': situacao,        # Ex: Em andamento (Como na planilha)
                    'VALOR_OBRA': _valor_br_para_float(valor_obra),
                    'DATA_INICIO': formatar_data_br(data_inicio),
                    'CONCLUSAO_PREVISTA': formatar_data_br(conclusao_prevista),
                    'DATA_FINALIZACAO': formatar_data_br(data_finalizacao) if data_finalizacao else "—",
                    'NOME_EMPRESA': nome_empresa,
                    'CNPJ_EMPRESA': cnpj_empresa,
                    'EMPRESA_CONTRATADA': empresa_completa, # Padronizado para o Looker
                    'TIPO_EXECUCAO': tipo_execucao,  # Ex: Nova Construção (Como na planilha)
                    'ENDERECO': endereco,
                    'URL_FOTO': url_da_foto_capa,
                    'GALERIA': urls_galeria,
                    'DATA_CADASTRO': agora.strftime('%d/%m/%Y %H:%M:%S'),
                    'TIMESTAMP_CADASTRO': agora
                }

                try:
                    resultado = colecao.insert_one(nova_obra)
                    break
                except DuplicateKeyError:
                    if id_obra_manual:
                        return _render_cadastro(f"O ID {id_obra_manual} já está cadastrado no banco de dados.", nivel='error')
                    continue  # outro cadastro pegou esse número primeiro: recalcula e tenta de novo

            if resultado is None:
                messages.error(request, "Não foi possível gerar um ID de obra único. Tente novamente.")
                return redirect('Cadastro-Obras')

            # 7. HISTÓRICO (TIMELAPSE) — só agora que o ID final está confirmado
            try:
                colecao_historico = colecao_timelapse
                registros = [{
                    'ID_OBRA': id_obra_gerado,
                    'URL_FOTO': url,
                    'SITUACAO': situacao,
                    'DATA_REGISTRO': formatar_data_br(str(agora.date())),
                    'TIMESTAMP': agora
                } for url in urls_galeria]
                if registros: colecao_historico.insert_many(registros)
            except Exception:
                logger.exception("Erro ao registrar timelapse da obra")

            # 8. SALVAR NO GOOGLE SHEETS (em segundo plano: a planilha é só espelhamento
            # best-effort e não deve atrasar a resposta ao usuário)
            _disparar_em_background(salvar_no_google_sheets, {k: v for k, v in nova_obra.items() if k != '_id'})
            _bump_cache_obras()
            messages.success(request, f"Obra {id_obra_gerado} salva com sucesso.")
            return redirect('Cadastro-Obras')

        except Exception:
            logger.exception("Erro ao cadastrar obra")
            messages.error(request, "Erro inesperado ao cadastrar a obra. Tente novamente.")
            return redirect('Cadastro-Obras')

    contexto_token = {'form_token': _gerar_form_token()}
    dados_repostos = request.session.pop('erro_cadastro_obras', None)
    if dados_repostos:
        contexto_token['dados'] = dados_repostos

    if request.headers.get('HX-Request'):
        return render(request, 'cadastro_obras.html', contexto_token)
    return render(request, 'index.html', {'template_meio': 'cadastro_obras.html', **contexto_token})

def busca_atualiza_obra(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')


    if request.method == 'POST':
        id_obra_pesquisado = request.POST.get('ID_OBRA', '').strip()

        if not id_obra_pesquisado:
            messages.error(request, "Informe um ID de obra para buscar.")
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_obra.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})

        # Procura a obra específica no banco
        obra_encontrada = colecao_obras.find_one({'ID_OBRA': id_obra_pesquisado})

        if obra_encontrada:
            # Convertendo as datas para o formulário entender
            obra_encontrada['DATA_INICIO'] = preparar_data_para_input(obra_encontrada.get('DATA_INICIO'))
            obra_encontrada['CONCLUSAO_PREVISTA'] = preparar_data_para_input(obra_encontrada.get('CONCLUSAO_PREVISTA'))
            obra_encontrada['DATA_FINALIZACAO'] = preparar_data_para_input(obra_encontrada.get('DATA_FINALIZACAO'))
            # VALOR_OBRA é float no banco — converter para string BR para o campo de texto do form
            obra_encontrada['VALOR_OBRA'] = _float_para_br(obra_encontrada.get('VALOR_OBRA', 0))

            # Retorna o template de edição
            contexto = {'obra': obra_encontrada, 'form_token': _gerar_form_token()}
            if request.headers.get('HX-Request'):
                return render(request, 'edita_obra.html', contexto)
            return render(request, 'index.html', {'template_meio': 'edita_obra.html', **contexto})
        else:
            messages.error(request, "Nenhuma obra encontrada com este ID.")
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_obra.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})
        
    if request.headers.get('HX-Request'):
        return render(request, 'busca_atualiza_obra.html')
    
    return render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})


def salva_edicao_obra(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    if request.method == 'POST':
        def _render_edicao(msg):
            messages.warning(request, msg)
            # Guarda os campos (texto) na sessão e redireciona — evita reenvio do
            # POST (e das fotos) num F5. Fotos novas em si não dá pra repor.
            request.session['erro_edicao_obra'] = request.POST.dict()
            return redirect('salva_edicao_obra')

        if _form_ja_em_processamento(request, 'salva_edicao_obra'):
            return _render_edicao("Já recebemos uma solicitação recente. Evite enviar mais de uma vez.")

        if _token_ja_usado(request.POST.get('form_token', '')):
            return _render_edicao("Esta edição já foi enviada. Se precisar editar novamente, refaça a busca.")

        # Coleta fora do try para que erros de validação nunca sejam engolidos pelo except externo
        id_obra = request.POST.get('ID_OBRA', '').strip()
        tipo_obra = request.POST.get('TIPO_OBRA', '').strip()
        situacao = request.POST.get('SITUACAO', '').strip()
        valor_obra = request.POST.get('VALOR_OBRA', '').strip()
        data_inicio = request.POST.get('DATA_INICIO', '').strip()
        conclusao_prevista = request.POST.get('CONCLUSAO_PREVISTA', '').strip()
        data_finalizacao = request.POST.get('DATA_FINALIZACAO', '').strip()
        nome_empresa = request.POST.get('NOME_EMPRESA', '').strip()
        cnpj_empresa = request.POST.get('CNPJ_EMPRESA', '').strip()
        tipo_execucao = request.POST.get('TIPO_EXECUCAO', '').strip()
        endereco = request.POST.get('ENDERECO', '').strip()
        fotos_novas_arquivos = request.FILES.getlist('FOTO_OBRA')

        # ==========================================
        # VALIDAÇÕES (fora do try — nunca engolidas pelo except)
        # ==========================================
        campos_obrigatorios = [
            id_obra, tipo_obra, situacao, valor_obra, data_inicio,
            conclusao_prevista, nome_empresa, cnpj_empresa,
            tipo_execucao, endereco
        ]

        if any(not campo for campo in campos_obrigatorios):
            return _render_edicao("Preencha todos os campos obrigatórios antes de salvar.")

        if len(fotos_novas_arquivos) > MAX_FOTOS_POR_ENVIO:
            return _render_edicao(f"Envie no máximo {MAX_FOTOS_POR_ENVIO} fotos por vez.")

        if fotos_novas_arquivos:
            obra_atual = colecao_obras.find_one({'ID_OBRA': id_obra}, {'GALERIA': 1})
            galeria_atual = obra_atual.get('GALERIA', []) if obra_atual else []
            if len(galeria_atual) + len(fotos_novas_arquivos) > MAX_FOTOS_POR_ENVIO:
                return _render_edicao(
                    f"A galeria já possui {len(galeria_atual)} foto(s). "
                    f"O limite é {MAX_FOTOS_POR_ENVIO} fotos no total."
                )

        for foto in fotos_novas_arquivos:
            erro_foto = _validar_foto(foto)
            if erro_foto:
                return _render_edicao(erro_foto)

        if not validar_cnpj(cnpj_empresa.replace('.', '').replace('/', '').replace('-', '')):
            return _render_edicao("O CNPJ informado é inválido.")

        if not valor_e_valido(valor_obra):
            return _render_edicao("O Valor Total da Obra informado é inválido.")

        if situacao not in SITUACOES_VALIDAS:
            return _render_edicao("A Situação selecionada é inválida.")

        if tipo_execucao not in TIPOS_EXECUCAO_VALIDOS:
            return _render_edicao("O Tipo de Execução selecionado é inválido.")

        if not texto_tem_letra(tipo_obra) or not texto_tem_letra(nome_empresa) or not texto_tem_letra(endereco):
            return _render_edicao("Tipo de Obra, Nome da Empresa e Endereço não podem conter apenas números ou símbolos.")

        if not data_e_valida(data_inicio, tipo="obra") or not data_e_valida(conclusao_prevista, tipo="obra"):
            return _render_edicao("A Data de Início ou a Conclusão Prevista contém um ano inválido ou irreal.")

        if data_finalizacao and not data_e_valida(data_finalizacao, tipo="obra"):
            return _render_edicao("A Data de Finalização informada é inválida ou irreal.")

        dt_inicio = datetime.strptime(data_inicio, '%Y-%m-%d').date()
        dt_conclusao = datetime.strptime(conclusao_prevista, '%Y-%m-%d').date()

        if dt_inicio >= dt_conclusao:
            return _render_edicao("A Conclusão Prevista deve ser posterior à Data de Início.")

        if data_finalizacao:
            dt_finalizacao = datetime.strptime(data_finalizacao, '%Y-%m-%d').date()
            if dt_inicio >= dt_finalizacao:
                return _render_edicao("A Data de Finalização deve ser posterior à Data de Início.")

        # Verifica que a obra existe ANTES de travar o lock — evita bloquear o
        # usuário por 12s após um erro de "não encontrada".
        if not colecao_obras.find_one({'ID_OBRA': id_obra}, {'_id': 1}):
            return _render_edicao(f"Obra {id_obra} não encontrada. Refaça a busca.")

        # Trava contra duplo submit só depois da validação de existência.
        _marcar_form_em_processamento(request, 'salva_edicao_obra', segundos=12)

        erro_foto = False
        resultado_update = None
        try:
            # 4. PREPARAÇÃO DO DICIONÁRIO DE ATUALIZAÇÃO (Campos de Texto)
            empresa_completa = f"{nome_empresa.upper()} - CNPJ - {cnpj_empresa}"
            agora_edicao = datetime.now(timezone.utc)

            dados_atualizados = {
                'TIPO_OBRA': tipo_obra.upper(),
                'SITUACAO': situacao, # Padronizado
                'VALOR_OBRA': _valor_br_para_float(valor_obra),
                'DATA_INICIO': formatar_data_br(data_inicio),
                'CONCLUSAO_PREVISTA': formatar_data_br(conclusao_prevista),
                'DATA_FINALIZACAO': formatar_data_br(data_finalizacao) if data_finalizacao else "—",
                'NOME_EMPRESA': nome_empresa, # Padronizado
                'CNPJ_EMPRESA': cnpj_empresa,
                'EMPRESA_CONTRATADA': empresa_completa,
                'TIPO_EXECUCAO': tipo_execucao, # Padronizado
                'ENDERECO': endereco, # Padronizado
                'DATA_ULTIMA_ATUALIZACAO': agora_edicao.strftime('%d/%m/%Y %H:%M:%S'),
                'TIMESTAMP_ULTIMA_ATUALIZACAO': agora_edicao
            }

            colecao = colecao_obras
            colecao_historico = colecao_timelapse

            # ==========================================
            # 6. PROCESSAMENTO DAS NOVAS FOTOS (Se houver)
            # ==========================================
            urls_novas = []
            registros_historico = []
            public_ids_novos = []
            if fotos_novas_arquivos:
                try:
                    for foto in fotos_novas_arquivos:
                        resultado_upload = _upload_com_retry(foto)
                        url_url = resultado_upload.get('secure_url')
                        if not url_url:
                            raise ValueError("Cloudinary não retornou 'secure_url' para o upload.")
                        public_ids_novos.append(resultado_upload.get('public_id', ''))
                        urls_novas.append(url_url)

                    # Só atualiza capa e timelapse depois que TODOS os uploads terminaram com sucesso.
                    dados_atualizados['URL_FOTO'] = urls_novas[0]

                    registros_historico = []
                    data_registro_br = formatar_data_br(str(agora_edicao.date()))
                    for url in urls_novas:
                        registros_historico.append({
                            'ID_OBRA': id_obra,
                            'URL_FOTO': url,
                            'SITUACAO': situacao,
                            'DATA_REGISTRO': data_registro_br,
                            'TIMESTAMP': agora_edicao
                        })
                    # insert_many ocorre abaixo, após update_one confirmado.

                except Exception:
                    logger.exception("Erro ao processar imagens da obra")
                    # Limpa uploads já realizados antes de abortar — evita órfãos no Cloudinary.
                    for pid in public_ids_novos:
                        if pid:
                            try:
                                cloudinary.uploader.destroy(pid)
                            except Exception:
                                pass
                    erro_foto = True
                    # Garante que nenhuma URL parcial chegue ao update do Mongo.
                    urls_novas = []
                    dados_atualizados.pop('URL_FOTO', None)

            # ==========================================
            # 7. EXECUTANDO OS UPDATES NO MONGODB
            # ==========================================

            # Update 1: Atualiza os campos normais ($set)
            resultado_update = colecao.update_one({'ID_OBRA': id_obra}, {'$set': dados_atualizados})

            if resultado_update.matched_count > 0:
                # Cache bumped aqui, antes do $push, para garantir invalidação
                # mesmo que o upload das fotos novas lance uma exceção.
                _bump_cache_obras()

                # Update 2: Adiciona as novas URLs ao Array de Galeria ($push)
                if urls_novas:
                    colecao.update_one(
                        {'ID_OBRA': id_obra},
                        {'$push': {'GALERIA': {'$each': urls_novas}}}
                    )
                    # Timelapse inserido só após update confirmado, evitando registros órfãos.
                    if registros_historico:
                        try:
                            colecao_historico.insert_many(registros_historico)
                        except Exception:
                            logger.exception("Erro ao registrar timelapse da obra %s", id_obra)

                # ==========================================
                # 8. ATUALIZAÇÃO NO GOOGLE SHEETS (em segundo plano)
                # ==========================================
                dados_para_sheets = dados_atualizados.copy()
                dados_para_sheets['ID_OBRA'] = id_obra
                _disparar_em_background(atualizar_no_google_sheets, dados_para_sheets)

        except Exception:
            logger.exception("Erro crítico ao salvar edição de obra")
            if erro_foto:
                messages.error(request, "Erro ao salvar as alterações e ao processar as imagens. Tente novamente.")
            else:
                messages.error(request, "Erro inesperado ao salvar as alterações. Tente novamente.")
            return redirect('salva_edicao_obra')

        # Fora do try/except — messages e redirect não são operações de DB e não
        # devem ficar dentro de um except amplo (ver CLAUDE.md).
        if resultado_update is None or resultado_update.matched_count == 0:
            # A obra existia no find_one acima mas sumiu antes do update (deleção concorrente).
            # Limpa uploads já feitos — sem documento salvo, ficam órfãos no Cloudinary.
            for pid in public_ids_novos:
                if pid:
                    try:
                        cloudinary.uploader.destroy(pid)
                    except Exception:
                        logger.warning("Falha ao limpar foto órfã do Cloudinary após matched_count=0: %s", pid)
            messages.error(request, f"Obra {id_obra} não encontrada ao salvar. Refaça a busca.")
            return redirect('salva_edicao_obra')

        if erro_foto:
            messages.warning(request, f"Obra {id_obra} atualizada, mas houve erro ao processar as novas imagens. Tente enviar as fotos novamente.")
        else:
            messages.success(request, f"Obra {id_obra} atualizada com sucesso.")
        return redirect('salva_edicao_obra')

    # Retorno adequado para HTMX ou acesso direto (GET, ou após o redirect acima)
    dados_repostos = request.session.pop('erro_edicao_obra', None)
    if dados_repostos:
        contexto_erro = {'obra': dados_repostos, 'form_token': _gerar_form_token()}
        if request.headers.get('HX-Request'):
            return render(request, 'edita_obra.html', contexto_erro)
        return render(request, 'index.html', {'template_meio': 'edita_obra.html', **contexto_erro})

    if request.headers.get('HX-Request'):
        return render(request, 'busca_atualiza_obra.html')
    return render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})



def lista_obras(request):
    # Busca as obras paginadas (mais recentes primeiro), evitando carregar a coleção inteira.
    # Resultado fica em cache por CACHE_TTL_SEGUNDOS, invalidado automaticamente a cada
    # cadastro/edição de obra (via _bump_cache_obras), para não reler o Mongo a cada visita.
    pagina_num = request.GET.get('pagina', 1)

    try:
        pagina_num = int(pagina_num)
    except (TypeError, ValueError):
        pagina_num = 1
    pagina_num = max(1, pagina_num)

    versao_cache = cache.get(CACHE_KEY_VERSAO_OBRAS, 0)

    # Chave provisória — pode ser recalculada se pagina_num for clampeado abaixo.
    chave_cache = f'lista_obras_v{versao_cache}_p{pagina_num}'
    contexto = cache.get(chave_cache)

    if contexto is None:
        total_obras = colecao_obras.count_documents({})
        total_paginas = max(1, -(-total_obras // _OBRAS_POR_PAGINA_PUBLICA))
        pagina_num = max(1, min(pagina_num, total_paginas))
        # Atualiza a chave para o valor real após clamp, garantindo hit em próximo acesso.
        chave_cache = f'lista_obras_v{versao_cache}_p{pagina_num}'

        obras = list(
            colecao_obras.find({}, _CAMPOS_LISTA_OBRAS)
            .sort('TIMESTAMP_CADASTRO', -1)
            .skip((pagina_num - 1) * _OBRAS_POR_PAGINA_PUBLICA)
            .limit(_OBRAS_POR_PAGINA_PUBLICA)
        )
        for o in obras:
            o['VALOR_OBRA'] = _float_para_br(o.get('VALOR_OBRA', 0))

        contexto = {
            'obras': obras,
            'pagina_atual': pagina_num,
            'total_paginas': total_paginas,
            'tem_anterior': pagina_num > 1,
            'tem_proxima': pagina_num < total_paginas,
        }
        cache.set(chave_cache, contexto, _CACHE_TTL_LISTA_OBRAS)

    if request.headers.get('HX-Request'):
        return render(request, 'lista_obras.html', contexto)
    return render(request, 'index.html', {'template_meio': 'lista_obras.html', **contexto})

def galeria_obra(request, id_obra):
    try:
        # 1. Busca a obra pelo ID
        obra = colecao_obras.find_one({"ID_OBRA": str(id_obra)}, {'GALERIA': 1, 'URL_FOTO': 1, 'DATA_CADASTRO': 1, '_id': 0})
        
        fotos_com_data = []
        if obra:
            # 3. Pega as URLs da galeria ou a foto de capa
            urls = obra.get('GALERIA', [])
            if not urls and obra.get('URL_FOTO'):
                urls = [obra.get('URL_FOTO')]
            
            # 4. Data padrão (Plano B: se a foto não tiver no timelapse, usa a data que a obra foi cadastrada)
            data_padrao = obra.get('DATA_CADASTRO', 'Desconhecida')
            data_padrao_curta = data_padrao.split()[0] if data_padrao != 'Desconhecida' else data_padrao
            
            # 5. Busca TODAS as datas dessa obra no Timelapse e cria um "Dicionário de Datas"
            historicos = colecao_timelapse.find({"ID_OBRA": str(id_obra)}, {"URL_FOTO": 1, "DATA_REGISTRO": 1, "_id": 0})
            mapa_datas = {h.get('URL_FOTO'): h.get('DATA_REGISTRO') for h in historicos if h.get('URL_FOTO')}
            
            # 6. Cruza a URL com a Data encontrada
            for url in urls:
                data_foto = mapa_datas.get(url, data_padrao_curta)
                fotos_com_data.append({
                    'url': url,
                    'data': data_foto
                })
                
        # 7. Renderiza o modal passando as fotos + datas
        return render(request, 'modal_galeria.html', {'fotos': fotos_com_data, 'id_obra': id_obra})
    
    except Exception:
        logger.exception("Erro ao carregar galeria da obra")
        return render(request, 'modal_galeria.html', {'fotos': [], 'id_obra': id_obra})

def salvar_no_google_sheets(dados):
    # 1. Configura as credenciais
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = GoogleServiceAccountCredentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
    client = gspread.authorize(creds)

    # 2. Abre a planilha
    planilha = client.open(GOOGLE_SHEETS_SPREADSHEET_NAME).sheet1

    # ==========================================
    # 3. TRAVA DE DUPLICAÇÃO PARA OBRAS LEGADAS
    # ==========================================
    id_procurado = str(dados.get('ID_OBRA'))
    celula_existente = planilha.find(id_procurado, in_column=1)
    
    if celula_existente:
        return "EXISTIA"
    # ==========================================

    # 4. Prepara a linha na ordem EXATA da imagem da planilha
    linha = [
        dados.get('ID_OBRA'),                   # Coluna A: ID
        _float_para_br(dados.get('VALOR_OBRA', 0)),  # Coluna B: Valor Total (Sem o R$)
        dados.get('DATA_INICIO'),               # Coluna C: Data Início
        dados.get('CONCLUSAO_PREVISTA'),        # Coluna D: Conclusão Prevista
        dados.get('DATA_FINALIZACAO'),          # Coluna E: Data Finalização
        
        # Coluna F: Tipo de Execução (Vem do HTML: "Nova Construção", "Reforma")
        dados.get('TIPO_EXECUCAO'), 
        
        # Coluna G: Tipo de Obra (Sempre MAIÚSCULO na planilha)
        dados.get('TIPO_OBRA').upper() if dados.get('TIPO_OBRA') else '',
        
        # Coluna H: Situação (Vem do HTML: "Em andamento", "Em licitação")
        dados.get('SITUACAO'), 
        
        # Coluna I: Empresa Contratada (Sempre MAIÚSCULO na planilha)
        dados.get('EMPRESA_CONTRATADA').upper() if dados.get('EMPRESA_CONTRATADA') else '',
        
        # Coluna J: Endereço (Sempre MAIÚSCULO na planilha)
        dados.get('ENDERECO').upper() if dados.get('ENDERECO') else ''
    ]

    # 5. Adiciona na planilha
    planilha.append_row(linha)
    
    return "CRIADA"

def atualizar_no_google_sheets(dados):
    # Sem try/except externo — exceções propagam para _disparar_em_background,
    # que retenta a chamada até TENTATIVAS_BACKGROUND vezes. Espelha o padrão
    # de salvar_no_google_sheets.
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = GoogleServiceAccountCredentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
    client = gspread.authorize(creds)

    planilha = client.open(GOOGLE_SHEETS_SPREADSHEET_NAME).sheet1

    id_procurado = str(dados.get('ID_OBRA'))
    celula = planilha.find(id_procurado, in_column=1)

    if not celula:
        # Condição permanente (obra nunca foi sincronizada ou foi deletada da planilha)
        # — não lança exceção para evitar que _disparar_em_background retente 3x inutilmente.
        logger.warning("Obra ID %s não foi encontrada na Planilha do Google — sync pendente.", id_procurado)
        return

    numero_da_linha = celula.row

    linha_atualizada = [
        dados.get('ID_OBRA'),
        _float_para_br(dados.get('VALOR_OBRA', 0)),
        dados.get('DATA_INICIO'),
        dados.get('CONCLUSAO_PREVISTA'),
        dados.get('DATA_FINALIZACAO'),
        dados.get('TIPO_EXECUCAO'),
        dados.get('TIPO_OBRA').upper() if dados.get('TIPO_OBRA') else '',
        dados.get('SITUACAO'),
        dados.get('EMPRESA_CONTRATADA').upper() if dados.get('EMPRESA_CONTRATADA') else '',
        dados.get('ENDERECO').upper() if dados.get('ENDERECO') else '',
    ]

    intervalo = f'A{numero_da_linha}:J{numero_da_linha}'
    planilha.update(values=[linha_atualizada], range_name=intervalo)
    return True


def deletar_do_google_sheets(id_obra):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = GoogleServiceAccountCredentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
    client = gspread.authorize(creds)
    planilha = client.open(GOOGLE_SHEETS_SPREADSHEET_NAME).sheet1
    celula = planilha.find(str(id_obra), in_column=1)
    if not celula:
        logger.warning("Obra ID %s não encontrada na planilha ao tentar excluir.", id_obra)
        return
    planilha.delete_rows(celula.row)


def _extrair_public_id_cloudinary(url):
    """Extrai o public_id de uma URL Cloudinary para permitir exclusão."""
    try:
        partes = url.split('/upload/')
        if len(partes) < 2:
            return None
        # Transformações Cloudinary: segmentos com vírgula (c_fill,w_300) ou
        # prefixo curto de 1-3 letras seguido de _ (c_, w_, h_, q_, f_, t_, etc.)
        # Nomes de pasta não seguem esse padrão — a regex é conservadora.
        segmentos = partes[1].split('/')
        inicio = 0
        for i, seg in enumerate(segmentos):
            if _RE_CLOUDINARY_VERSAO.match(seg):
                inicio = i + 1
                break
            if _RE_CLOUDINARY_TRANSFORM.search(seg):
                inicio = i + 1
            else:
                inicio = i
                break
        public_id = '/'.join(segmentos[inicio:]).rsplit('.', 1)[0]
        return public_id or None
    except Exception:
        return None


def alterar_cargo_funcionario(request):
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')
    if not request.user.is_superuser:
        messages.error(request, "Acesso restrito ao Gerente Geral.")
        return redirect('zona_admin')
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    form_token = request.POST.get('form_token', '').strip()
    if _token_ja_usado(form_token):
        messages.error(request, "Ação já processada ou token inválido.")
        return redirect('zona_admin')

    cpf = request.POST.get('cpf', '').strip()
    novo_cargo = request.POST.get('novo_cargo', '').strip().upper()

    if novo_cargo not in ('COMUM', 'SUPERVISOR'):
        messages.error(request, "Cargo inválido.")
        return redirect('zona_admin')

    if cpf == request.user.username:
        messages.error(request, "Não é possível alterar o próprio cargo.")
        return redirect('zona_admin')

    try:
        usuario_alvo = User.objects.get(username=cpf)
    except User.DoesNotExist:
        messages.error(request, "Usuário não encontrado.")
        return redirect('zona_admin')

    if usuario_alvo.is_superuser:
        messages.error(request, "Não é possível alterar o cargo de outro Gerente Geral.")
        return redirect('zona_admin')

    novo_is_staff = (novo_cargo == 'SUPERVISOR')
    is_staff_original = usuario_alvo.is_staff
    usuario_alvo.is_staff = novo_is_staff
    usuario_alvo.save(update_fields=['is_staff'])

    try:
        resultado_cargo = colecao_funcionarios.update_one({'CPF': cpf}, {'$set': {'FUNCAO': novo_cargo}})
    except Exception:
        try:
            usuario_alvo.is_staff = is_staff_original
            usuario_alvo.save(update_fields=['is_staff'])
        except Exception:
            logger.exception("CRÍTICO: falha ao reverter is_staff após erro MongoDB — CPF=%s", cpf)
        logger.exception("Erro ao alterar cargo do funcionário CPF=%s", cpf)
        messages.error(request, "Erro ao alterar cargo. Tente novamente.")
        return redirect('zona_admin')

    if resultado_cargo.matched_count == 0:
        logger.warning(
            "alterar_cargo: sem documento Mongo para CPF=%s — is_staff atualizado no Django, "
            "FUNCAO não encontrada no Mongo (usuário criado fora do fluxo normal?)", cpf
        )

    nome_func = usuario_alvo.first_name or cpf
    cargo_label = 'Supervisor' if novo_cargo == 'SUPERVISOR' else 'Funcionário Comum'
    messages.success(request, f"Cargo de {nome_func} alterado para {cargo_label}.")
    logger.warning(
        "Cargo alterado: CPF=%s (%s) → %s | executado por %s (IP: %s)",
        cpf, nome_func, cargo_label, request.user.username, _ip_do_cliente(request)
    )

    if request.headers.get('HX-Request'):
        resp = HttpResponse(status=204)
        resp['HX-Redirect'] = reverse('zona_admin') + '?aba=funcionarios'
        return resp
    return redirect(reverse('zona_admin') + '?aba=funcionarios')


def deletar_funcionario(request):
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')
    if not request.user.is_superuser:
        messages.error(request, "Acesso restrito ao Gerente Geral.")
        return redirect('zona_admin')
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    form_token = request.POST.get('form_token', '').strip()
    if _token_ja_usado(form_token):
        messages.error(request, "Ação já processada ou token inválido.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    cpf = request.POST.get('cpf', '').strip()
    if not cpf:
        messages.error(request, "CPF inválido.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    if cpf == request.user.username:
        messages.error(request, "Não é possível excluir a própria conta.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    try:
        usuario_alvo = User.objects.get(username=cpf)
    except User.DoesNotExist:
        messages.error(request, "Usuário não encontrado.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    if usuario_alvo.is_superuser:
        messages.error(request, "Não é possível excluir outro Gerente Geral.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    nome = usuario_alvo.first_name or cpf

    # Deleta o Django User primeiro — se falhar, o Mongo não é tocado.
    # Se o Mongo falhar depois, o usuário não consegue mais logar (Django User
    # já foi removido), deixando um documento órfão no Mongo em vez do contrário
    # (usuário ativo sem perfil), que seria mais grave.
    try:
        usuario_alvo.delete()
    except Exception:
        logger.exception("Erro ao deletar Django User para CPF=%s", cpf)
        messages.error(request, "Erro ao remover funcionário. Tente novamente.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    try:
        colecao_funcionarios.delete_one({'CPF': cpf})
    except Exception:
        logger.exception("Erro ao deletar documento Mongo para CPF=%s — Django User já removido", cpf)
        messages.error(request, "O acesso do funcionário foi revogado, mas houve erro ao remover os dados de perfil. Contate o suporte.")
        return redirect(reverse('zona_admin') + '?aba=funcionarios')

    logger.warning(
        "Funcionário %s (CPF=%s) excluído por %s (IP: %s)",
        nome, cpf, request.user.username, _ip_do_cliente(request)
    )
    messages.success(request, f"Funcionário {nome} excluído com sucesso.")

    if request.headers.get('HX-Request'):
        resp = HttpResponse(status=204)
        resp['HX-Redirect'] = reverse('zona_admin') + '?aba=funcionarios'
        return resp
    return redirect(reverse('zona_admin') + '?aba=funcionarios')


def zona_admin(request):
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    cargo = _cargo_usuario(request.user)
    aba = request.GET.get('aba', 'obras')

    # Aba funcionários: restrita a supervisor e gerente geral
    if aba == 'funcionarios' and cargo == 'COMUM':
        messages.error(request, "Acesso restrito a supervisores.")
        return redirect('zona_admin')

    try:
        pagina = max(1, int(request.GET.get('pagina', 1)))
    except (ValueError, TypeError):
        pagina = 1

    ctx = {
        'aba': aba,
        'cargo': cargo,
        'form_token_obra': _gerar_form_token(),
        'form_token_deletar_func': _gerar_form_token(),
        'form_token_cargo': _gerar_form_token(),
    }

    if aba == 'obras':
        total = colecao_obras.count_documents({})
        total_paginas = max(1, (total + _OBRAS_POR_PAGINA_ADMIN - 1) // _OBRAS_POR_PAGINA_ADMIN)
        pagina = min(pagina, total_paginas)
        skip = (pagina - 1) * _OBRAS_POR_PAGINA_ADMIN

        obras = list(colecao_obras.find(
            {},
            {'ID_OBRA': 1, 'TIPO_OBRA': 1, 'SITUACAO': 1, 'ENDERECO': 1, 'DATA_CADASTRO': 1, 'GALERIA': 1, '_id': 0}
        ).sort('TIMESTAMP_CADASTRO', -1).skip(skip).limit(_OBRAS_POR_PAGINA_ADMIN))

        ctx.update({'obras': obras, 'pagina': pagina, 'total_paginas': total_paginas, 'total': total})

    elif aba == 'funcionarios':
        funcionarios = list(colecao_funcionarios.find(
            {},
            {'NOME': 1, 'CPF': 1, 'FUNCAO': 1, 'DATA_CADASTRO': 1, '_id': 0}
        ).sort('DATA_CADASTRO', -1).limit(500))

        # Enriquece com is_staff/is_superuser do Django para exibir cargo real
        cpfs = [f.get('CPF') for f in funcionarios if f.get('CPF')]
        django_users = {u.username: u for u in User.objects.filter(username__in=cpfs)}
        for func in funcionarios:
            u = django_users.get(func.get('CPF'))
            if u and u.is_superuser:
                func['cargo_func'] = 'GERENTE_GERAL'
            elif u and u.is_staff:
                func['cargo_func'] = 'SUPERVISOR'
            else:
                func['cargo_func'] = 'COMUM'
            cpf_raw = func.get('CPF', '')
            if len(cpf_raw) == 11 and cpf_raw.isdigit():
                func['CPF_MASCARADO'] = f"{cpf_raw[:2]}*.***.***-**"
            else:
                func['CPF_MASCARADO'] = '***.***.***-**'

        ctx['funcionarios'] = funcionarios

    if request.headers.get('HX-Request'):
        return render(request, 'zona_admin.html', ctx)
    return render(request, 'index.html', {'template_meio': 'zona_admin.html', **ctx})


def deletar_obra(request):
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Acesso restrito a supervisores.")
        return redirect('inicio')
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    # Guard de token de idempotência — evita duplo-submit do modal de confirmação
    form_token = request.POST.get('form_token', '').strip()
    if _token_ja_usado(form_token):
        messages.error(request, "Ação já processada ou token inválido.")
        return redirect('zona_admin')

    id_obra = request.POST.get('id_obra', '').strip()
    if not id_obra:
        messages.error(request, "ID de obra inválido.")
        return redirect('zona_admin')

    obra = colecao_obras.find_one({'ID_OBRA': id_obra})
    if not obra:
        messages.error(request, f"Obra {id_obra} não encontrada.")
        return redirect('zona_admin')

    # 1. Deletar fotos do Cloudinary (melhor esforço — falha não impede exclusão)
    galeria = obra.get('GALERIA') or []
    for url in galeria:
        public_id = _extrair_public_id_cloudinary(url)
        if public_id:
            try:
                cloudinary.uploader.destroy(public_id)
            except Exception:
                logger.warning("Não foi possível deletar imagem Cloudinary: %s", public_id)

    # 2. Deletar entradas de timelapse
    try:
        colecao_timelapse.delete_many({'ID_OBRA': id_obra})
    except Exception:
        logger.exception("Erro ao deletar timelapse da obra %s", id_obra)

    # 3. Deletar da planilha em background
    _disparar_em_background(deletar_do_google_sheets, id_obra)

    # 4. Deletar a obra do MongoDB
    try:
        colecao_obras.delete_one({'ID_OBRA': id_obra})
    except Exception:
        logger.exception("Erro ao deletar obra %s do MongoDB", id_obra)
        messages.error(request, "Erro ao excluir a obra. Tente novamente.")
        return redirect('zona_admin')
    _bump_cache_obras()

    logger.warning(
        "Obra %s excluída por %s (IP: %s)",
        id_obra, request.user.username, _ip_do_cliente(request)
    )
    messages.success(request, f"Obra {id_obra} excluída com sucesso.")

    if request.headers.get('HX-Request'):
        response = HttpResponse(status=204)
        response['HX-Redirect'] = reverse('zona_admin')
        return response
    return redirect('zona_admin')