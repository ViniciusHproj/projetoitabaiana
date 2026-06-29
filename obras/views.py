import os
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from django.shortcuts import render, redirect
from pymongo import MongoClient
from django.contrib import messages
import ntplib
from datetime import datetime
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
import cloudinary.uploader
from django.contrib.auth.forms import AuthenticationForm
from functools import wraps
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.messages import get_messages # Adicione este import no topo
# Create your views here.

logger = logging.getLogger(__name__)

# Cliente único reaproveitado entre requests (pymongo já faz pool de conexões internamente).
# connect=False adia a resolução de DNS/conexão real para o primeiro uso, em vez de travar
# a inicialização do Django caso o banco esteja temporariamente inacessível.
_mongo_client = MongoClient(os.environ['MONGODB_URI'], connect=False)
_db = _mongo_client[os.environ['MONGODB_DB_NAME']]
colecao_obras = _db['Banco_Obras']
colecao_funcionarios = _db['Banco_funcionarios']
colecao_timelapse = _db['Banco_Timelapse']

GOOGLE_SHEETS_CREDENTIALS_FILE = os.environ.get('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credenciais.json')
GOOGLE_SHEETS_SPREADSHEET_NAME = os.environ.get('GOOGLE_SHEETS_SPREADSHEET_NAME', 'Data base OBRAS DE ITABIANA')

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

def login_view(request):
    if request.method == 'POST':
        usuario_cpf = request.POST.get('username', '').replace('.', '').replace('-', '')
        senha_digitada = request.POST.get('password', '')

        user = authenticate(request, username=usuario_cpf, password=senha_digitada)

        if user is not None:
            # 1. Faz o login
            auth_login(request, user)
            
                        
            system_messages = messages.get_messages(request)
            system_messages.used = True
            
            # 3. Agora sim, cria a mensagem única de boas-vindas
            nome_usuario = user.first_name.split()[0].title()
            funcao = "Supervisor" if user.is_staff else "Funcionário Comum"
            messages.success(request, f"✅ Autenticado com sucesso! Bem-vindo, {nome_usuario} ({funcao}).")

            proxima_pagina = request.GET.get('next', 'inicio')
            return redirect(proxima_pagina)
        else:
            # Se errar o login, também limpamos antes de mostrar o erro
            storage = get_messages(request)
            for _ in storage: pass
            
            messages.error(request, "⚠️ CPF ou Senha incorretos. Tente novamente.")
            return render(request, 'login.html')

    return render(request, 'login.html')
def logout_view(request):
    if request.user.is_authenticated:
        auth_logout(request)
        messages.info(request, "👋 Deslogando do sistema... Até logo!")
    return redirect('inicio') # Ou 'login', dependendo de onde você quer que ele caia

def staff_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        # 1. Se não está logado, manda pro login (comportamento normal)
        if not request.user.is_authenticated:
            return redirect(f'/?next={request.path}')
        
        # 2. Se está logado MAS não é STAFF, manda pro início com ERRO
        if not request.user.is_staff:
            messages.error(request, "🚫 Acesso não autorizado! Esta área é restrita a supervisores.")
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
    except:
        # Se a internet falhar, usamos o ano do sistema como plano B
        return datetime.now().year
    
def cadastro_funcionario(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    # 2. Bloqueio para quem está logado mas é COMUM (não é staff)
    if not request.user.is_staff:
        messages.error(request, "🚫 Acesso negado! Esta área é exclusiva para supervisores.")
        return redirect('inicio')
        
    if request.method == 'POST' and 'btn-salvar' in request.POST:
        try:
            def formatar_data_br(data_str):
                if not data_str:
                    return '—'
                try:
                    ano, mes, dia = data_str.split('-')
                    return f"{dia}/{mes}/{ano[:4]}"
                except:
                    return data_str
                    
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
                    messages.warning(request, "⚠️ Preencha todos os campos obrigatórios.")
                    return render(request, 'cadastro_funcionario.html', {'dados': request.POST})

            # ==========================================
            # 2.5 TRAVA DE SEGURANÇA DA DATA DE NASCIMENTO
            # ==========================================
            data_nasc_str = documento_funcionario['DATA_NASCIMENTO']
            
            # Chama a função validadora que criamos no topo do views.py
            if not data_e_valida(data_nasc_str, tipo="nascimento"):
                messages.warning(request, "⚠️ A data de nascimento informada é inválida ou irreal.")
                return render(request, 'cadastro_funcionario.html', {'dados': request.POST})
            # ==========================================

            # 3. VERIFICAÇÃO DE DUPLICIDADE (Evita crash no Django)
            cpf = documento_funcionario['CPF']
            if User.objects.filter(username=cpf).exists():
                messages.error(request, '⚠️ Erro: Já existe um funcionário cadastrado com este CPF.')
                return render(request, 'cadastro_funcionario.html', {'dados': request.POST})

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
            colecao_funcionarios.insert_one(documento_funcionario)

            messages.success(request, f"✅ Funcionário {nome} cadastrado com sucesso!")
            return redirect('cadastro_funcionario')

        except Exception:
            logger.exception("Erro ao cadastrar funcionário")
            messages.error(request, "⚠️ Erro inesperado ao cadastrar. Tente novamente.")
            return render(request, 'cadastro_funcionario.html', {'dados': request.POST})

    if request.headers.get('HX-Request'):
        return render(request, 'cadastro_funcionario.html')

    return render(request, 'index.html', {'template_meio': 'cadastro_funcionario.html'})



def busca_atualiza_funcionario(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    # 2. Bloqueio para quem está logado mas é COMUM (não é staff)
    if not request.user.is_staff:
        messages.error(request, "🚫 Acesso negado! Esta área é exclusiva para supervisores.")
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
            
            contexto = {'funcionario': funcionario}

            # Se for HTMX, manda só o formulário de edição
            if request.headers.get('HX-Request'):
                return render(request, 'edita_funcionario.html', contexto)
            
            # Se for acesso direto (F5 no resultado da busca), manda a index com o template de edição
            return render(request, 'index.html', {
                'template_meio': 'edita_funcionario.html', 
                **contexto
            })

        else:
            messages.error(request, "⚠️ Funcionário não encontrado.")
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
    if request.method == 'POST':
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
            messages.warning(request, "⚠️ Atenção: Não é permitido deixar Nome, RG, Função ou Data de Nascimento em branco.")
            
            # Mantém o fluxo correto do seu HTMX caso dê erro
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_funcionario.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})
        # ==========================================

        # ==========================================
        # 2.5 TRAVA DE SEGURANÇA DA DATA DE NASCIMENTO
        # ==========================================
        if not data_e_valida(data_input, tipo="nascimento"):
            messages.warning(request, "⚠️ A data de nascimento informada é inválida ou irreal.")
            
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_funcionario.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})
        # ==========================================

        # 3. Formata a data para o padrão BR (DD/MM/AAAA)
        data_br = "—"
        if data_input:
            try:
                a, m, d = data_input.split('-')
                data_br = f"{d}/{m}/{a}"
            except:
                data_br = data_input

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
            messages.error(request, "⚠️ Erro: Usuário não encontrado na tabela de autenticação.")
            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_funcionario.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})

        # 6. ATUALIZAÇÃO NO MONGODB
        # Nota: Não salvamos a senha no Mongo, conforme sua lógica anterior
        colecao_funcionarios.update_one({'CPF': cpf_original}, {'$set': dados_atualizados_mongo})

        messages.success(request, f"✅ Dados de {nome_novo} atualizados com sucesso!")
        
    # Retorno adequado para HTMX ou acesso direto
    if request.headers.get('HX-Request'):
        return render(request, 'busca_atualiza_funcionario.html')
    return render(request, 'index.html', {'template_meio': 'busca_atualiza_funcionario.html'})


# --- SUA VIEW DE CADASTRO DE OBRA ---


def cadastro_obras(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')

    if request.method == 'POST' and 'btn-salvar' in request.POST:
        try:
            # 1. COLETA E LIMPEZA DOS DADOS DO FORMULÁRIO
            id_obra_manual = request.POST.get('ID_OBRA_MANUAL', '').strip()
            
            # Mantemos o TIPO_OBRA em maiúsculo se esse for o padrão da sua planilha legada
            tipo_obra = request.POST.get('TIPO_OBRA', '').strip().upper() 
            
            # SITUACAO e TIPO_EXECUCAO agora vêm com acento e minúsculas do HTML
            situacao = request.POST.get('SITUACAO', '').strip()
            tipo_execucao = request.POST.get('TIPO_EXECUCAO', '').strip()
            
            valor_obra = request.POST.get('VALOR_OBRA', '').strip()
            data_inicio = request.POST.get('DATA_INICIO', '').strip()
            conclusao_prevista = request.POST.get('CONCLUSAO_PREVISTA', '').strip()
            data_finalizacao = request.POST.get('DATA_FINALIZACAO', '').strip()
            
            # Empresa e Endereço: removi o .upper() para ficar mais amigável, 
            # mas você pode manter se a planilha exigir maiúsculas.
            nome_empresa = request.POST.get('NOME_EMPRESA', '').strip()
            endereco = request.POST.get('ENDERECO', '').strip()
            
            cnpj_empresa = request.POST.get('CNPJ_EMPRESA', '').strip()
            fotos_arquivos = request.FILES.getlist('FOTO_OBRA')

            # ==========================================
            # 2. TRAVA DE SEGURANÇA (Campos Vazios)
            # ==========================================
            campos_obrigatorios = [
                tipo_obra, situacao, valor_obra, data_inicio, 
                conclusao_prevista, nome_empresa, cnpj_empresa, 
                tipo_execucao, endereco
            ]

            if any(not campo for campo in campos_obrigatorios) or not fotos_arquivos:
                messages.warning(request, "⚠️ Preencha todos os campos obrigatórios e envie pelo menos uma foto da obra.")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})

            # ==========================================
            # 2.5/2.6 TRAVAS DE SEGURANÇA (Datas e Cronologia)
            # ==========================================
            if not data_e_valida(data_inicio, tipo="obra") or not data_e_valida(conclusao_prevista, tipo="obra"):
                messages.warning(request, "⚠️ A Data de Início ou a Conclusão Prevista contém um ano inválido ou irreal.")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})
            
            if data_finalizacao and not data_e_valida(data_finalizacao, tipo="obra"):
                messages.warning(request, "⚠️ A Data de Finalização informada é inválida ou irreal.")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})

            if data_inicio > conclusao_prevista:
                messages.warning(request, "⚠️ A Data de Início não pode ser maior que a Conclusão Prevista.")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})

            if data_finalizacao and data_inicio > data_finalizacao:
                messages.warning(request, "⚠️ A Data de Início não pode ser maior que a Data de Finalização.")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})
            
            # 3. GERAÇÃO DE ID
            colecao = colecao_obras

            if id_obra_manual:
                id_obra_gerado = id_obra_manual
            else:
                ano_atual = pegar_ano_google()
                filtro_ano = {"ID_OBRA": {"$regex": f"{ano_atual}$"}}
                contagem_ano = colecao.count_documents(filtro_ano)
                proximo_numero = contagem_ano + 1
                id_obra_gerado = f"{proximo_numero}{ano_atual}"

            if colecao.find_one({"ID_OBRA": id_obra_gerado}):
                messages.error(request, f"⚠️ O ID {id_obra_gerado} já está cadastrado no Banco de Dados!")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})

            # 4. UPLOAD MÚLTIPLO (Cloudinary)
            urls_galeria = []
            try:
                for foto in fotos_arquivos:
                    resultado_upload = cloudinary.uploader.upload(foto, folder="obras_projeto")
                    urls_galeria.append(resultado_upload.get('secure_url'))
            except Exception:
                messages.error(request, "⚠️ Erro ao enviar as imagens para o servidor.")
                return render(request, 'cadastro_obras.html', {'dados': request.POST})

            url_da_foto_capa = urls_galeria[0] if urls_galeria else ""

            # 5. MONTAGEM DO DICIONÁRIO (Fiel ao Banco e Planilha)
            def formatar_data_br(data_str):
                if not data_str: return '—'
                try:
                    ano, mes, dia = data_str.split('-')
                    return f"{dia}/{mes}/{ano[:4]}"
                except: return data_str

            # Padronização da Empresa para a Planilha
            empresa_completa = f"{nome_empresa.upper()} - CNPJ - {cnpj_empresa}"
            agora = datetime.now()

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
            
            # 6. HISTÓRICO (TIMELAPSE)
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
            except Exception as e: print(f"Erro Timelapse: {e}")

            # 7. SALVAR NO MONGODB
            resultado = colecao.insert_one(nova_obra)

            # 8. SALVAR NO GOOGLE SHEETS
            if resultado.inserted_id:
                try:
                    status_planilha = salvar_no_google_sheets(nova_obra)
                    msg = f"✅ Obra {id_obra_gerado} salva no MongoDB!"
                    if status_planilha != "EXISTIA": msg += " E enviada à Planilha!"
                    messages.success(request, msg)
                except Exception:
                    logger.exception("Erro ao salvar obra na Planilha do Google")
                    messages.warning(request, "⚠️ Salvo no Banco, mas houve um erro ao enviar para a Planilha.")

                return redirect('Cadastro-Obras')

        except Exception:
            logger.exception("Erro ao cadastrar obra")
            messages.error(request, "⚠️ Erro inesperado ao cadastrar a obra. Tente novamente.")
            return redirect('Cadastro-Obras')

    if request.headers.get('HX-Request'):
        return render(request, 'cadastro_obras.html')
    return render(request, 'index.html', {'template_meio': 'cadastro_obras.html'})

def busca_atualiza_obra(request):
    # 1. Bloqueio para quem nem logou ainda
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={request.path}')


    if request.method == 'POST':
        id_obra_pesquisado = request.POST.get('ID_OBRA', '').strip()
        
        # Procura a obra específica no banco
        obra_encontrada = colecao_obras.find_one({'ID_OBRA': id_obra_pesquisado})

        if obra_encontrada:
            # --- FUNÇÃO REVERSA DA DATA ---
            # Transforma "DD/MM/AAAA" de volta para "AAAA-MM-DD"
            def preparar_data_para_input(data_br):
                if not data_br or data_br == '—':
                    return ""
                try:
                    dia, mes, ano = data_br.split('/')
                    return f"{ano}-{mes}-{dia}"
                except:
                    return ""

            # Convertendo as datas para o formulário entender
            obra_encontrada['DATA_INICIO'] = preparar_data_para_input(obra_encontrada.get('DATA_INICIO'))
            obra_encontrada['CONCLUSAO_PREVISTA'] = preparar_data_para_input(obra_encontrada.get('CONCLUSAO_PREVISTA'))
            obra_encontrada['DATA_FINALIZACAO'] = preparar_data_para_input(obra_encontrada.get('DATA_FINALIZACAO'))

            # Retorna o template de edição
            return render(request, 'edita_obra.html', {'obra': obra_encontrada})
        else:
            messages.error(request, "⚠️ Nenhuma obra encontrada com este ID.")
            return render(request, 'busca_atualiza_obra.html')
        
    if request.headers.get('HX-Request'):
        return render(request, 'busca_atualiza_obra.html')
    
    return render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})


def salva_edicao_obra(request):
    if request.method == 'POST':
        try:
            # 1. COLETA E LIMPEZA DOS DADOS DO FORMULÁRIO
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

            # --- MODIFICADO: Pega a lista de NOVAS fotos enviadas ---
            fotos_novas_arquivos = request.FILES.getlist('FOTO_OBRA')

            # ==========================================
            # 2. TRAVAS DE SEGURANÇA (Campos e Datas)
            # ==========================================
            campos_obrigatorios = [
                id_obra, tipo_obra, situacao, valor_obra, data_inicio,
                conclusao_prevista, nome_empresa, cnpj_empresa,
                tipo_execucao, endereco
            ]

            if any(not campo for campo in campos_obrigatorios):
                messages.warning(request, "⚠️ Preencha todos os campos obrigatórios antes de salvar.")
                return render(request, 'busca_atualiza_obra.html') if request.headers.get('HX-Request') else render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})

            if not data_e_valida(data_inicio, tipo="obra") or not data_e_valida(conclusao_prevista, tipo="obra"):
                messages.warning(request, "⚠️ Datas de Início ou Previsão inválidas.")
                return render(request, 'busca_atualiza_obra.html') if request.headers.get('HX-Request') else render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})

            # 3. FUNÇÃO INTERNA DE FORMATAÇÃO
            def formatar_para_db(data_str):
                if not data_str or data_str == '—': return '—'
                try:
                    ano, mes, dia = data_str.split('-')
                    return f"{dia}/{mes}/{ano[:4]}"
                except: return data_str

            # 4. PREPARAÇÃO DO DICIONÁRIO DE ATUALIZAÇÃO (Campos de Texto)
            empresa_completa = f"{nome_empresa.upper()} - CNPJ - {cnpj_empresa}"
            agora_edicao = datetime.now()
            
            dados_atualizados = {
                'TIPO_OBRA': tipo_obra.upper(),
                'SITUACAO': situacao, # Padronizado
                'VALOR_OBRA': valor_obra,
                'DATA_INICIO': formatar_para_db(data_inicio),
                'CONCLUSAO_PREVISTA': formatar_para_db(conclusao_prevista),
                'DATA_FINALIZACAO': formatar_para_db(data_finalizacao) if data_finalizacao else "—",
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
                    data_registro_br = formatar_para_db(str(agora_edicao.date()))
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
                    messages.error(request, "⚠️ Erro ao processar as imagens enviadas.")

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
            # 8. ATUALIZAÇÃO NO GOOGLE SHEETS
            # ==========================================
            try:
                dados_para_sheets = dados_atualizados.copy()
                dados_para_sheets['ID_OBRA'] = id_obra
                atualizar_no_google_sheets(dados_para_sheets)
                messages.success(request, f"✅ Obra {id_obra} atualizada com sucesso em todos os sistemas!")
            except Exception:
                logger.exception("Erro ao atualizar obra na Planilha do Google")
                messages.warning(request, "✅ Banco atualizado, mas houve um erro na Planilha.")

            if request.headers.get('HX-Request'):
                return render(request, 'busca_atualiza_obra.html')
            return render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})

        except Exception:
            logger.exception("Erro crítico ao salvar edição de obra")
            messages.error(request, "⚠️ Erro inesperado ao salvar as alterações. Tente novamente.")
            return render(request, 'busca_atualiza_obra.html') if request.headers.get('HX-Request') else render(request, 'index.html', {'template_meio': 'busca_atualiza_obra.html'})



def lista_obras(request):
    # Busca as obras paginadas (mais recentes primeiro), evitando carregar a coleção inteira
    pagina_num = request.GET.get('pagina', 1)
    TAMANHO_PAGINA = 12

    total_obras = colecao_obras.count_documents({})
    try:
        pagina_num = int(pagina_num)
    except (TypeError, ValueError):
        pagina_num = 1
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
    
    except Exception as e:
        print(f"Erro ao carregar galeria: {e}")
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
            print(f"Obra ID {id_procurado} não foi encontrada na Planilha do Google.")
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
        
    except Exception as e:
        print(f"⚠️ Erro ao atualizar Google Sheets: {e}")
        return False