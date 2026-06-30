import os
import logging
import threading
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from django.shortcuts import render, redirect
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from django.contrib import messages
from django.core.cache import cache
import ntplib
import uuid
from datetime import datetime, timedelta
from django.contrib.auth.models import User
import cloudinary.uploader
from functools import wraps
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.messages import get_messages # Adicione este import no topo
from obras.utils import formatar_data_br, preparar_data_para_input
# Create your views here.

logger = logging.getLogger(__name__)


def _disparar_em_background(funcao, *args, **kwargs):
    """
    Executa uma chamada de rede "melhor esforço" (ex: Google Sheets) numa thread
    separada, sem bloquear a resposta ao usuário. Falhas são apenas logadas —
    o Mongo já é a fonte de verdade, a planilha é só espelhamento.
    """
    def _executar():
        try:
            funcao(*args, **kwargs)
        except Exception:
            logger.exception("Erro ao executar tarefa em segundo plano: %s", funcao.__name__)

    threading.Thread(target=_executar, daemon=True).start()


CACHE_KEY_VERSAO_OBRAS = 'obras_cache_versao'


def _bump_cache_obras():
    """Invalida o cache de lista_obras após qualquer cadastro/edição de obra."""
    try:
        cache.incr(CACHE_KEY_VERSAO_OBRAS)
    except ValueError:
        cache.set(CACHE_KEY_VERSAO_OBRAS, 1)

# Cliente único reaproveitado entre requests (pymongo já faz pool de conexões internamente).
# connect=False adia a resolução de DNS/conexão real para o primeiro uso, em vez de travar
# a inicialização do Django caso o banco esteja temporariamente inacessível.
_mongo_client = MongoClient(os.environ['MONGODB_URI'], connect=False)
_db = _mongo_client[os.environ['MONGODB_DB_NAME']]
colecao_obras = _db['Banco_Obras']
colecao_funcionarios = _db['Banco_funcionarios']
colecao_timelapse = _db['Banco_Timelapse']
colecao_seguranca_login = _db['Banco_SegurancaLogin']

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


def _ip_do_cliente(request):
    """Pega o IP real do request. Em produção (Render) o tráfego passa por um
    proxy reverso, que define X-Forwarded-For — sem isso, REMOTE_ADDR seria
    sempre o IP do proxy, não do usuário, e o rate limit nunca bloquearia ninguém."""
    encaminhado = request.META.get('HTTP_X_FORWARDED_FOR')
    if encaminhado:
        return encaminhado.split(',')[0].strip()
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
    restante = (doc['expira_em'] - datetime.utcnow()).total_seconds()
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


def _registrar_tentativa_falha_login(request, chave_ip, chave_cpf, cpf_tentado):
    agora = datetime.utcnow()
    expira_em = agora + timedelta(seconds=LOGIN_JANELA_BLOQUEIO_SEGUNDOS)

    contagem_ip, _ = _ler_contador_login(chave_ip)
    nova_contagem_ip = contagem_ip + 1
    colecao_seguranca_login.update_one(
        {'_id': chave_ip},
        {'$set': {'contagem': nova_contagem_ip, 'expira_em': expira_em, 'atualizado_em': agora}},
        upsert=True,
    )

    nova_contagem_cpf = 0
    if chave_cpf:
        contagem_cpf, _ = _ler_contador_login(chave_cpf)
        nova_contagem_cpf = contagem_cpf + 1
        colecao_seguranca_login.update_one(
            {'_id': chave_cpf},
            {'$set': {'contagem': nova_contagem_cpf, 'expira_em': expira_em, 'atualizado_em': agora}},
            upsert=True,
        )

    if max(nova_contagem_ip, nova_contagem_cpf) >= LOGIN_MAX_TENTATIVAS:
        logger.warning(
            "Login bloqueado por excesso de tentativas — IP=%s CPF=%s",
            _ip_do_cliente(request), cpf_tentado or '(vazio)'
        )
        if chave_cpf:
            _registrar_bloqueio_repetido(cpf_tentado)


def _registrar_bloqueio_repetido(cpf_tentado):
    """Conta quantas vezes essa conta foi bloqueada nas últimas 24h. Vários
    bloqueios seguidos na mesma conta sugerem um ataque direcionado a um
    usuário específico (não só um erro de digitação ocasional)."""
    chave_alerta = f'login_alerta_cpf_{cpf_tentado}'
    agora = datetime.utcnow()
    expira_em = agora + timedelta(hours=24)

    doc = colecao_seguranca_login.find_one({'_id': chave_alerta})
    contagem_atual = doc.get('contagem', 0) if doc and doc['expira_em'] > agora else 0
    nova_contagem = contagem_atual + 1

    colecao_seguranca_login.update_one(
        {'_id': chave_alerta},
        {'$set': {'contagem': nova_contagem, 'expira_em': expira_em, 'atualizado_em': agora}},
        upsert=True,
    )

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


def _form_ja_em_processamento(request, nome_acao, segundos=4):
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
    que venha rápido o suficiente para escapar da trava de tempo acima."""
    if not token:
        return False  # sem token (ex: chamada antiga/externa) não bloqueia, só não protege
    chave = f'form_token_{token}'
    if cache.get(chave):
        return True
    cache.set(chave, True, 300)  # 5 minutos é mais que suficiente para qualquer reenvio
    return False

def pagina_inicial(request):
    return render(request, 'index.html')
 # Aqui você diz qual arquivo abrir
def index(request):
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
        ano_atual = datetime.now().year

        # Regra 1: Absolutamente nenhuma data no sistema pode ser antes de 1900
        if data_obj.year < 1900:
            return False

        # Regra 2: Para funcionários (Não pode nascer no futuro e limite de 120 anos)
        if tipo == "nascimento":
            if data_obj.year > ano_atual or (ano_atual - data_obj.year) > 120:
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


def login_view(request):
    # Só mostra o aviso de ?aviso= na carga inicial (GET). O <form> desta página
    # reenvia para request.get_full_path(), que inclui essa mesma query string —
    # se checássemos isso também no POST, a mensagem seria recriada a cada
    # tentativa de login e duplicaria com a de sucesso/erro do POST.
    if request.method == 'GET':
        aviso = request.GET.get('aviso', '')
        if aviso == 'inatividade':
            messages.warning(request, "Sua sessão foi encerrada automaticamente por inatividade. Faça login novamente.")
            return redirect('login')
        elif aviso == 'saiu':
            messages.info(request, "Sessão encerrada com sucesso.")
            return redirect('login')
        return render(request, 'login.html')

    if request.method == 'POST':
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


            system_messages = messages.get_messages(request)
            system_messages.used = True

            # 3. Agora sim, cria a mensagem única de boas-vindas
            nome_usuario = user.first_name.split()[0].title()
            funcao = "Supervisor" if user.is_staff else "Funcionário Comum"
            messages.success(request, f"Autenticado com sucesso. Bem-vindo, {nome_usuario} ({funcao}).")

            proxima_pagina = request.GET.get('next', 'inicio')
            return redirect(proxima_pagina)
        else:
            _registrar_tentativa_falha_login(request, chave_ip, chave_cpf, usuario_cpf)
            restantes = _tentativas_restantes(chave_ip, chave_cpf)
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

    return render(request, 'login.html')
def logout_view(request):
    motivo = request.GET.get('motivo', '')
    if request.user.is_authenticated:
        auth_logout(request)
    if motivo == 'inatividade':
        return redirect(f"/login/?aviso=inatividade")
    return redirect(f"/login/?aviso=saiu")

def staff_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        # 1. Se não está logado, manda pro login (comportamento normal)
        if not request.user.is_authenticated:
            return redirect(f'/?next={request.path}')
        
        # 2. Se está logado MAS não é STAFF, manda pro início com ERRO
        if not request.user.is_staff:
            messages.error(request, "Acesso não autorizado. Esta área é restrita a supervisores.")
            return redirect('inicio') # Redireciona para a rota do Dashboard
            
        return view_func(request, *args, **kwargs)
    return _wrapped_view
def eh_supervisor(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)

def pegar_ano_google():
    try:
        # Tenta pegar a hora real de um servidor de tempo (NTP)
        cliente_ntp = ntplib.NTPClient()
        resposta = cliente_ntp.request('pool.ntp.org', version=3)
        return datetime.fromtimestamp(resposta.tx_time).year
    except Exception:
        # Se a internet falhar, usamos o ano do sistema como plano B
        return datetime.now().year
    
def cadastro_funcionario(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    # 2. Bloqueio para quem está logado mas é COMUM (não é staff)
    if not request.user.is_staff:
        messages.error(request, "Acesso negado. Esta área é exclusiva para supervisores.")
        return redirect('inicio')
        
    def _render_cadastro_func(msg, nivel='warning'):
        getattr(messages, nivel)(request, msg)
        # Guarda os campos digitados na sessão (não dá pra repor depois de um
        # redirect de outra forma) e redireciona — em vez de devolver a página
        # direto do POST, o que faria o navegador reenviar o formulário a cada F5.
        request.session['erro_cadastro_funcionario'] = request.POST.dict()
        return redirect('cadastro_funcionario')

    if request.method == 'POST' and 'btn-salvar' in request.POST:
        if _form_ja_em_processamento(request, 'cadastro_funcionario'):
            return _render_cadastro_func("Sua solicitação já está sendo processada. Aguarde alguns segundos.")

        if _token_ja_usado(request.POST.get('form_token', '')):
            return _render_cadastro_func("Este cadastro já foi enviado. Se precisar cadastrar outro funcionário, recarregue a página.")

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
            # ==========================================

            # ==========================================
            # 2.6 TRAVA DE SEGURANÇA DO CPF (dígito verificador)
            # ==========================================
            if not validar_cpf(documento_funcionario['CPF']):
                return _render_cadastro_func("O CPF informado é inválido.")
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
            documento_funcionario['DATA_CADASTRO'] = datetime.now()
            
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

    contexto_token = {'form_token': _gerar_form_token()}
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
    if not request.user.is_staff:
        messages.error(request, "Acesso negado. Esta área é exclusiva para supervisores.")
        return redirect('inicio')
    # Lógica para o POST (Quando o usuário clica em "Buscar")
    if request.method == 'POST':
        cpf_pesquisado = request.POST.get('CPF', '').strip()
        cpf_limpo = cpf_pesquisado.replace('.', '').replace('-', '')
            
        funcionario = colecao_funcionarios.find_one({'CPF': cpf_limpo})

        if funcionario:
            # --- CONVERSÃO DA DATA (BR para HTML) ---
            data_nasc = funcionario.get('DATA_NASCIMENTO', '')
            if data_nasc and '/' in data_nasc:
                d, m, a = data_nasc.split('/')
                funcionario['DATA_NASCIMENTO'] = f"{a}-{m}-{d}"
            
            contexto = {'funcionario': funcionario, 'form_token': _gerar_form_token()}

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
    if not request.user.is_staff:
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
            messages.warning(request, "Sua solicitação já está sendo processada. Aguarde alguns segundos.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        if _token_ja_usado(request.POST.get('form_token', '')):
            messages.warning(request, "Esta edição já foi enviada. Se precisar editar novamente, refaça a busca.")
            request.session['erro_edicao_funcionario'] = _dados_para_repor(request)
            return redirect('salva_edicao_funcionario')

        cpf_original = request.POST.get('CPF_ORIGINAL')

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
        # ==========================================

        # Só trava contra duplo submit agora que passamos por todas as validações.
        _marcar_form_em_processamento(request, 'salva_edicao_funcionario')

        # 3. Formata a data para o padrão BR (DD/MM/AAAA)
        data_br = formatar_data_br(data_input)

        # 4. Prepara os dados para o MongoDB
        nome_novo = nome_novo.upper()
        funcao_nova = funcao_nova.upper()
        rg_limpo = rg_novo.replace('.', '').replace('-', '')

        dados_atualizados_mongo = {
            'NOME': nome_novo,
            'DATA_NASCIMENTO': data_br,
            'RG': rg_limpo,
            'FUNCAO': funcao_nova,
        }

        # 5. ATUALIZAÇÃO NA TABELA DE AUTENTICAÇÃO (DJANGO)
        try:
            # Localiza o usuário no Django pelo CPF (username)
            usuario_django = User.objects.get(username=cpf_original)
            
            # Atualiza o Nome
            usuario_django.first_name = nome_novo
            
            # Atualiza o Nível de Acesso (Staff) baseado na Função
            if funcao_nova == 'SUPERVISOR':
                usuario_django.is_staff = True
            else:
                usuario_django.is_staff = False
            
            # Atualiza a senha apenas se foi digitada
            nova_senha = request.POST.get('SENHA')
            if nova_senha and nova_senha.strip() != "":
                usuario_django.set_password(nova_senha) # O set_password já faz o hash automático!
            
            usuario_django.save()

        except User.DoesNotExist:
            messages.error(request, "Usuário não encontrado na tabela de autenticação.")
            return redirect('salva_edicao_funcionario')

        # 6. ATUALIZAÇÃO NO MONGODB
        # Nota: Não salvamos a senha no Mongo, conforme sua lógica anterior
        colecao_funcionarios.update_one({'CPF': cpf_original}, {'$set': dados_atualizados_mongo})

        messages.success(request, f"Dados de {nome_novo} atualizados com sucesso.")
        # Redirect (PRG) — sem isso, um F5 depois de salvar reenviaria o POST
        # e tentaria salvar a edição de novo.
        return redirect('salva_edicao_funcionario')

    # Retorno adequado para HTMX ou acesso direto (GET, ou após o redirect acima)
    dados_repostos = request.session.pop('erro_edicao_funcionario', None)
    if dados_repostos:
        contexto_erro = {'funcionario': dados_repostos, 'form_token': _gerar_form_token()}
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
            return _render_cadastro("Sua solicitação já está sendo processada. Aguarde alguns segundos.")

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

        if not validar_cnpj(cnpj_empresa.replace('.', '').replace('/', '').replace('-', '')):
            return _render_cadastro("O CNPJ informado é inválido.")

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

        # Só trava contra duplo submit agora — e por mais tempo que os outros
        # formulários, porque o upload de fotos pro Cloudinary pode demorar
        # vários segundos em conexão ruim (4s seria curto demais aqui).
        _marcar_form_em_processamento(request, 'cadastro_obras', segundos=12)

        try:
            # 4. UPLOAD MÚLTIPLO (Cloudinary)
            colecao = colecao_obras
            urls_galeria = []
            try:
                for foto in fotos_arquivos:
                    resultado_upload = cloudinary.uploader.upload(foto, folder="obras_projeto")
                    urls_galeria.append(resultado_upload.get('secure_url'))
            except Exception:
                return _render_cadastro("Erro ao enviar as imagens para o servidor.", nivel='error')

            url_da_foto_capa = urls_galeria[0] if urls_galeria else ""

            # 5. MONTAGEM DO DICIONÁRIO (Fiel ao Banco e Planilha)

            # Padronização da Empresa para a Planilha
            empresa_completa = f"{nome_empresa.upper()} - CNPJ - {cnpj_empresa}"
            agora = datetime.now()
            ano_atual = pegar_ano_google()

            # ==========================================
            # 6. GERAÇÃO DE ID + SALVAMENTO ATÔMICO
            # ==========================================
            # O número usado é sempre recalculado a partir da contagem real de obras do
            # ano no momento da tentativa — por isso não fica "buraco" na numeração quando
            # uma obra é excluída. Um índice único em ID_OBRA garante que, se duas
            # requisições colidirem no mesmo número ao mesmo tempo, o MongoDB rejeita a
            # segunda inserção (DuplicateKeyError) e o código recalcula e tenta de novo
            # automaticamente, sem nunca duplicar um ID.
            resultado = None
            id_obra_gerado = id_obra_manual or None

            for _tentativa in range(10):
                if not id_obra_manual:
                    contagem_ano = colecao.count_documents({"ID_OBRA": {"$regex": f"{ano_atual}$"}})
                    id_obra_gerado = f"{contagem_ano + 1}{ano_atual}"

                nova_obra = {
                    'ID_OBRA': id_obra_gerado,
                    'TIPO_OBRA': tipo_obra,      # Ex: PAVIMENTAÇÃO (Maiúsculo)
                    'SITUACAO': situacao,        # Ex: Em andamento (Como na planilha)
                    'VALOR_OBRA': valor_obra,
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
            if resultado.inserted_id:
                _disparar_em_background(salvar_no_google_sheets, nova_obra)
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
        
        # Procura a obra específica no banco
        obra_encontrada = colecao_obras.find_one({'ID_OBRA': id_obra_pesquisado})

        if obra_encontrada:
            # Convertendo as datas para o formulário entender
            obra_encontrada['DATA_INICIO'] = preparar_data_para_input(obra_encontrada.get('DATA_INICIO'))
            obra_encontrada['CONCLUSAO_PREVISTA'] = preparar_data_para_input(obra_encontrada.get('CONCLUSAO_PREVISTA'))
            obra_encontrada['DATA_FINALIZACAO'] = preparar_data_para_input(obra_encontrada.get('DATA_FINALIZACAO'))

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
            return _render_edicao("Sua solicitação já está sendo processada. Aguarde alguns segundos.")

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

        if not validar_cnpj(cnpj_empresa.replace('.', '').replace('/', '').replace('-', '')):
            return _render_edicao("O CNPJ informado é inválido.")

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

        # Só trava contra duplo submit agora — e por mais tempo, pois a edição
        # também pode reenviar fotos novas pro Cloudinary.
        _marcar_form_em_processamento(request, 'salva_edicao_obra', segundos=12)

        try:
            # 4. PREPARAÇÃO DO DICIONÁRIO DE ATUALIZAÇÃO (Campos de Texto)
            empresa_completa = f"{nome_empresa.upper()} - CNPJ - {cnpj_empresa}"
            agora_edicao = datetime.now()

            dados_atualizados = {
                'TIPO_OBRA': tipo_obra.upper(),
                'SITUACAO': situacao, # Padronizado
                'VALOR_OBRA': valor_obra,
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
            if fotos_novas_arquivos:
                try:
                    for foto in fotos_novas_arquivos:
                        resultado_upload = cloudinary.uploader.upload(foto, folder="obras_projeto")
                        url_url = resultado_upload.get('secure_url')
                        urls_novas.append(url_url)
                    
                    # Atualizamos a CAPA da obra para a primeira das novas fotos enviadas
                    dados_atualizados['URL_FOTO'] = urls_novas[0]
                    
                    # REGISTRO NO TIMELAPSE (Um documento para cada foto nova)
                    registros_historico = []
                    data_registro_br = formatar_data_br(str(agora_edicao.date()))
                    for url in urls_novas:
                        registros_historico.append({
                            'ID_OBRA': id_obra,
                            'URL_FOTO': url,
                            'SITUACAO': situacao, # Padronizado (sem o .upper())
                            'DATA_REGISTRO': data_registro_br,
                            'TIMESTAMP': agora_edicao
                        })
                    colecao_historico.insert_many(registros_historico)
                    
                except Exception:
                    logger.exception("Erro ao processar imagens da obra")
                    messages.error(request, "Erro ao processar as imagens enviadas.")

            # ==========================================
            # 7. EXECUTANDO OS UPDATES NO MONGODB
            # ==========================================
            
            # Update 1: Atualiza os campos normais ($set)
            colecao.update_one({'ID_OBRA': id_obra}, {'$set': dados_atualizados})

            # Update 2: Adiciona as novas URLs ao Array de Galeria ($push)
            if urls_novas:
                colecao.update_one(
                    {'ID_OBRA': id_obra},
                    {'$push': {'GALERIA': {'$each': urls_novas}}}
                )

            # ==========================================
            # 8. ATUALIZAÇÃO NO GOOGLE SHEETS (em segundo plano, mesma lógica do cadastro)
            # ==========================================
            dados_para_sheets = dados_atualizados.copy()
            dados_para_sheets['ID_OBRA'] = id_obra
            _disparar_em_background(atualizar_no_google_sheets, dados_para_sheets)
            _bump_cache_obras()
            messages.success(request, f"Obra {id_obra} atualizada com sucesso.")
            # Redirect (PRG) — sem isso, um F5 depois de salvar reenviaria o POST
            # e tentaria salvar a edição (e reenviar fotos) de novo.
            return redirect('salva_edicao_obra')

        except Exception:
            logger.exception("Erro crítico ao salvar edição de obra")
            messages.error(request, "Erro inesperado ao salvar as alterações. Tente novamente.")
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
    TAMANHO_PAGINA = 12
    CACHE_TTL_SEGUNDOS = 120

    try:
        pagina_num = int(pagina_num)
    except (TypeError, ValueError):
        pagina_num = 1
    pagina_num = max(1, pagina_num)

    versao_cache = cache.get(CACHE_KEY_VERSAO_OBRAS, 0)
    chave_cache = f'lista_obras_v{versao_cache}_p{pagina_num}'
    contexto = cache.get(chave_cache)

    if contexto is None:
        total_obras = colecao_obras.count_documents({})
        total_paginas = max(1, -(-total_obras // TAMANHO_PAGINA))
        pagina_num = max(1, min(pagina_num, total_paginas))

        obras = list(
            colecao_obras.find()
            .sort('TIMESTAMP_CADASTRO', -1)
            .skip((pagina_num - 1) * TAMANHO_PAGINA)
            .limit(TAMANHO_PAGINA)
        )

        contexto = {
            'obras': obras,
            'pagina_atual': pagina_num,
            'total_paginas': total_paginas,
            'tem_anterior': pagina_num > 1,
            'tem_proxima': pagina_num < total_paginas,
        }
        cache.set(chave_cache, contexto, CACHE_TTL_SEGUNDOS)

    # Renderiza a página completa
    return render(request, 'lista_obras.html', contexto)

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
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS_FILE, scope)
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
        dados.get('VALOR_OBRA'),                # Coluna B: Valor Total (Sem o R$)
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
    try:
        # 1. Configura as credenciais
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)

        # 2. Abre a planilha
        planilha = client.open(GOOGLE_SHEETS_SPREADSHEET_NAME).sheet1

        # ID que vamos procurar na planilha
        id_procurado = str(dados.get('ID_OBRA'))
        
        # 3. Procura a célula (O gspread novo retorna None se não achar)
        celula = planilha.find(id_procurado, in_column=1)
        
        # Verifica se realmente achou a célula antes de tentar pegar a linha
        if not celula:
            logger.warning("Obra ID %s não foi encontrada na Planilha do Google.", id_procurado)
            return False
            
        numero_da_linha = celula.row
        
        # 4. Prepara os dados formatados (Fiel à imagem da planilha)
        linha_atualizada = [
            dados.get('ID_OBRA'),
            dados.get('VALOR_OBRA'), # Valor apenas em números, sem R$
            dados.get('DATA_INICIO'),
            dados.get('CONCLUSAO_PREVISTA'),
            dados.get('DATA_FINALIZACAO'),
            
            # Sem capitalize para manter o padrão "Nova Construção", "Reforma", etc.
            dados.get('TIPO_EXECUCAO'), 
            
            # Sempre Maiúsculo
            dados.get('TIPO_OBRA').upper() if dados.get('TIPO_OBRA') else '',
            
            # Sem capitalize para manter o padrão "Em andamento", "Cancelada", etc.
            dados.get('SITUACAO'), 
            
            # Sempre Maiúsculo
            dados.get('EMPRESA_CONTRATADA').upper() if dados.get('EMPRESA_CONTRATADA') else '',
            
            # Sempre Maiúsculo
            dados.get('ENDERECO').upper() if dados.get('ENDERECO') else ''
        ]

        # 5. Atualiza a linha específica na planilha
        intervalo = f'A{numero_da_linha}:J{numero_da_linha}'
        
        # Sobrescreve apenas a linha encontrada
        planilha.update(values=[linha_atualizada], range_name=intervalo)
        
        return True
        
    except Exception:
        logger.exception("Erro ao atualizar Google Sheets")
        return False