"""
Suíte de testes automatizados do app `obras`.

Particularidade importante deste projeto (ver CLAUDE.md "Data layer"): a
maior parte da lógica de negócio NÃO passa pelo ORM do Django — fala direto
com o MongoDB via `pymongo` (colecao_obras, colecao_funcionarios, etc.,
criadas uma única vez no topo de obras/views.py, plugadas no MONGODB_URI/
MONGODB_DB_NAME reais). O `TestCase` padrão do Django só cria banco de teste
isolado para o que passa pelo ORM (User/sessions) — as coleções de negócio
continuariam apontando para o banco de PRODUÇÃO se nada fosse feito.

Por isso, `MongoTesteBase` abaixo conecta num banco de teste separado (mesmo
cluster, nome com sufixo `_teste`) e usa `unittest.mock.patch` para trocar as
referências dessas coleções dentro do módulo `obras.views` durante os testes
— sem mudar nada em como a aplicação roda normalmente. O banco de teste é
limpo antes de cada teste e destruído ao final da suíte.

Requer conectividade real com o MongoDB configurado em MONGODB_URI (mesmo
cluster do .env) — não é um mock de rede, é um banco de teste de verdade.
"""
import os
from unittest.mock import patch

from django.contrib.auth.models import User, update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import Client, RequestFactory, TestCase
from pymongo import MongoClient

from unittest.mock import MagicMock

from obras import views as obras_views
from obras.utils import formatar_data_br, preparar_data_para_input
from obras.views import (
    _extrair_public_id_cloudinary,
    _gerar_form_token,
    _ip_do_cliente,
    data_e_valida,
    texto_tem_letra,
    valor_e_valido,
    validar_cnpj,
    validar_cpf,
    validar_rg,
)

# CPF e CNPJ válidos (dígito verificador real), usados só como fixture de teste.
CPF_VALIDO = "11144477735"
CNPJ_VALIDO = "11222333000181"
SENHA_FORTE = "S3nhaForteTeste!2026"

# JPEG mínimo válido (header SOI + APP0 + EOI) — passa na validação de magic bytes.
_JPEG_MINIMO = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xd9"
)


class ValidadoresTestCase(TestCase):
    """Funções puras — sem Mongo, sem mocks, sem rede."""

    def test_cpf_valido_aceito(self):
        self.assertTrue(validar_cpf(CPF_VALIDO))

    def test_cpf_com_sequencia_repetida_rejeitado(self):
        self.assertFalse(validar_cpf("11111111111"))

    def test_cpf_com_digito_verificador_errado_rejeitado(self):
        self.assertFalse(validar_cpf("11144477736"))

    def test_cpf_tamanho_errado_rejeitado(self):
        self.assertFalse(validar_cpf("123"))

    def test_cnpj_valido_aceito(self):
        self.assertTrue(validar_cnpj(CNPJ_VALIDO))

    def test_cnpj_com_digito_verificador_errado_rejeitado(self):
        self.assertFalse(validar_cnpj("11222333000180"))

    def test_rg_so_numeros_aceito(self):
        self.assertTrue(validar_rg("123456789"))

    def test_rg_com_letra_rejeitado(self):
        self.assertFalse(validar_rg("12345678X"))

    def test_valor_positivo_formato_br_aceito(self):
        self.assertTrue(valor_e_valido("1.234,56"))

    def test_valor_zero_rejeitado(self):
        self.assertFalse(valor_e_valido("0,00"))

    def test_valor_nao_numerico_rejeitado(self):
        self.assertFalse(valor_e_valido("abc"))

    def test_texto_com_letra_aceito(self):
        self.assertTrue(texto_tem_letra("Rua das Flores"))

    def test_texto_so_numeros_rejeitado(self):
        self.assertFalse(texto_tem_letra("12345"))

    def test_data_nascimento_futura_rejeitada(self):
        self.assertFalse(data_e_valida("2099-01-01", tipo="nascimento"))

    def test_data_nascimento_plausivel_aceita(self):
        self.assertTrue(data_e_valida("1990-05-20", tipo="nascimento"))

    def test_data_obra_ano_absurdo_rejeitado(self):
        self.assertFalse(data_e_valida("2200-01-01", tipo="obra"))

    def test_data_invalida_no_calendario_rejeitada(self):
        self.assertFalse(data_e_valida("2026-02-31", tipo="geral"))


class IpDoClienteTestCase(TestCase):
    """`_ip_do_cliente` — cobre a correção de segurança do X-Forwarded-For
    (deve usar o ÚLTIMO valor da lista, escrito pelo proxy confiável, nunca
    o primeiro, que o próprio cliente pode forjar)."""

    def setUp(self):
        self.rf = RequestFactory()

    def test_usa_ultimo_valor_do_x_forwarded_for(self):
        req = self.rf.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 9.9.9.9")
        self.assertEqual(_ip_do_cliente(req), "9.9.9.9")

    def test_um_unico_valor_no_x_forwarded_for(self):
        req = self.rf.get("/", HTTP_X_FORWARDED_FOR="8.8.8.8")
        self.assertEqual(_ip_do_cliente(req), "8.8.8.8")

    def test_sem_x_forwarded_for_usa_remote_addr(self):
        req = self.rf.get("/")
        self.assertEqual(_ip_do_cliente(req), req.META.get("REMOTE_ADDR", "desconhecido"))


class MongoTesteBase(TestCase):
    """
    Base para testes que precisam das coleções de negócio (pymongo puro).
    Conecta num banco de teste separado no mesmo cluster do .env e substitui,
    durante a classe de teste, as referências em `obras.views` — a view
    continua chamando `colecao_obras.insert_one(...)` normalmente, só que
    esse nome agora aponta para o banco de teste.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Com --keepdb, o banco de teste é preservado entre execuções. Se o
        # último teste de uma execução anterior deixou usuários Django no banco
        # de teste, a próxima execução falharia com IntegrityError (dup key) ao
        # tentar recriar o mesmo usuário. Flush garante estado limpo para a
        # classe inteira antes de começar, sem depender do teardown da execução
        # anterior ter funcionado.
        from django.core.management import call_command
        call_command("flush", "--no-input", verbosity=0)
        # Avaliado aqui (não no corpo da classe) para não quebrar import sem .env.
        cls.NOME_BANCO_TESTE = f"{os.environ['MONGODB_DB_NAME']}_teste"
        cls._mongo_client_teste = MongoClient(os.environ["MONGODB_URI"])
        db_teste = cls._mongo_client_teste[cls.NOME_BANCO_TESTE]

        cls.colecao_obras = db_teste["Banco_Obras"]
        cls.colecao_funcionarios = db_teste["Banco_funcionarios"]
        cls.colecao_timelapse = db_teste["Banco_Timelapse"]
        cls.colecao_seguranca_login = db_teste["Banco_SegurancaLogin"]
        cls.colecao_sessoes_ativas = db_teste["Banco_SessoesAtivas"]

        # Mesmos índices da produção (ID_OBRA único, TTL de bloqueio de login)
        # para que o comportamento testado seja fiel ao real.
        cls.colecao_obras.create_index("ID_OBRA", unique=True)
        cls.colecao_seguranca_login.create_index("expira_em", expireAfterSeconds=0)

        # update_last_login tenta chamar user.save(update_fields=["last_login"])
        # após cada force_login/login. Com django_mongodb_backend + Django 6, isso
        # falha com User.NotUpdated. O signal foi registrado com dispatch_uid=
        # 'update_last_login', então é preciso usar o mesmo uid para desconectar.
        user_logged_in.disconnect(dispatch_uid='update_last_login')

        # Troca as coleções que obras/views.py usa globalmente, só durante
        # esta classe de teste.
        cls._patches = [
            patch.object(obras_views, "colecao_obras", cls.colecao_obras),
            patch.object(obras_views, "colecao_funcionarios", cls.colecao_funcionarios),
            patch.object(obras_views, "colecao_timelapse", cls.colecao_timelapse),
            patch.object(obras_views, "colecao_seguranca_login", cls.colecao_seguranca_login),
            patch.object(obras_views, "colecao_sessoes_ativas", cls.colecao_sessoes_ativas),
            # Nunca disparar thread real de sincronização com Google Sheets nos testes.
            patch.object(obras_views, "_disparar_em_background", lambda funcao, *a, **kw: None),
        ]
        for p in cls._patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patches:
            p.stop()
        cls._mongo_client_teste.drop_database(cls.NOME_BANCO_TESTE)
        cls._mongo_client_teste.close()
        # Reconecta com o mesmo dispatch_uid original para manter idempotência.
        user_logged_in.connect(update_last_login, dispatch_uid='update_last_login')
        super().tearDownClass()

    def setUp(self):
        # Recria o client a cada teste para garantir que não há cookies de
        # sessão stale da execução anterior. Depois de um flush(), as sessões
        # Django são apagadas do banco; se o client reutilizado ainda tiver o
        # cookie antigo, force_login tenta fazer force_update numa sessão que
        # não existe mais → Session.NotUpdated.
        self.client = Client()
        # Isola cada teste: começa sempre com as coleções de negócio e usuários
        # Django vazios. O MongoDB não suporta rollback real de transações como
        # bancos SQL, então o Django não consegue desfazer INSERTs de User
        # automaticamente entre testes — sem esse delete explícito, o segundo
        # setUp() de uma mesma classe tenta criar um User com o mesmo username
        # que o primeiro já inseriu, gerando IntegrityError.
        User.objects.all().delete()
        self.colecao_obras.delete_many({})
        self.colecao_funcionarios.delete_many({})
        self.colecao_timelapse.delete_many({})
        self.colecao_seguranca_login.delete_many({})
        self.colecao_sessoes_ativas.delete_many({})


class LoginRateLimitTestCase(MongoTesteBase):
    def setUp(self):
        super().setUp()
        self.cpf = CPF_VALIDO
        self.user = User.objects.create_user(
            username=self.cpf, password=SENHA_FORTE, first_name="Joao Teste"
        )

    def test_login_com_credenciais_corretas_funciona(self):
        resposta = self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertTrue(self.client.session.get("_auth_user_id"))
        # Login bem-sucedido agora renderiza modal de confirmação (200) em vez de redirecionar.
        self.assertEqual(resposta.status_code, 200)

    def test_login_com_senha_errada_nao_autentica(self):
        resposta = self.client.post("/login/", {"username": self.cpf, "password": "senha-errada"})
        self.assertFalse(self.client.session.get("_auth_user_id"))
        self.assertRedirects(resposta, "/login/")

    def test_bloqueia_apos_exceder_tentativas(self):
        for _ in range(obras_views.LOGIN_MAX_TENTATIVAS):
            self.client.post("/login/", {"username": self.cpf, "password": "senha-errada"})

        # Mesmo com a senha CORRETA, a conta deve continuar bloqueada.
        resposta = self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertFalse(self.client.session.get("_auth_user_id"))
        mensagens = [str(m) for m in resposta.wsgi_request._messages]
        self.assertTrue(any("bloqueado" in m.lower() or "tentativas" in m.lower() for m in mensagens))

    def test_cpf_bloqueado_e_rejeitado_mesmo_de_ip_diferente(self):
        """CPF com 5 falhas fica bloqueado independentemente do IP de origem."""
        for _ in range(obras_views.LOGIN_MAX_TENTATIVAS):
            self.client.post(
                "/login/",
                {"username": self.cpf, "password": "senha-errada"},
                HTTP_X_FORWARDED_FOR="1.1.1.1",
            )
        # Mesmo vindo de um IP completamente diferente, o CPF ainda está bloqueado.
        resposta = self.client.post(
            "/login/",
            {"username": self.cpf, "password": SENHA_FORTE},
            HTTP_X_FORWARDED_FOR="9.9.9.9",
        )
        self.assertFalse(self.client.session.get("_auth_user_id"))
        mensagens = [str(m) for m in resposta.wsgi_request._messages]
        self.assertTrue(any("bloqueado" in m.lower() or "tentativas" in m.lower() for m in mensagens))

    def test_ip_bloqueado_impede_outros_cpfs_do_mesmo_ip(self):
        """IP com 5 falhas bloqueia qualquer conta tentada a partir dele."""
        for _ in range(obras_views.LOGIN_MAX_TENTATIVAS):
            self.client.post(
                "/login/",
                {"username": self.cpf, "password": "senha-errada"},
                HTTP_X_FORWARDED_FOR="1.1.1.1",
            )
        outro_cpf = "52998224725"
        User.objects.create_user(username=outro_cpf, password=SENHA_FORTE, first_name="Maria Teste")
        # IP 1.1.1.1 está bloqueado — outra conta tentada do mesmo IP também falha.
        resposta = self.client.post(
            "/login/",
            {"username": outro_cpf, "password": SENHA_FORTE},
            HTTP_X_FORWARDED_FOR="1.1.1.1",
        )
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_conta_diferente_de_ip_diferente_nao_e_afetada(self):
        """Bloqueio de um CPF/IP não afeta conta distinta em IP distinto."""
        for _ in range(obras_views.LOGIN_MAX_TENTATIVAS):
            self.client.post(
                "/login/",
                {"username": self.cpf, "password": "senha-errada"},
                HTTP_X_FORWARDED_FOR="1.1.1.1",
            )
        outro_cpf = "52998224725"
        User.objects.create_user(username=outro_cpf, password=SENHA_FORTE, first_name="Maria Teste")
        resposta = self.client.post(
            "/login/",
            {"username": outro_cpf, "password": SENHA_FORTE},
            HTTP_X_FORWARDED_FOR="2.2.2.2",
        )
        self.assertTrue(self.client.session.get("_auth_user_id"))
        self.assertEqual(resposta.status_code, 200)


class SessaoUnicaTestCase(MongoTesteBase):
    def setUp(self):
        super().setUp()
        self.cpf = CPF_VALIDO
        User.objects.create_user(username=self.cpf, password=SENHA_FORTE, first_name="Joao Teste")

    def test_segundo_login_derruba_a_primeira_sessao(self):
        cliente_1 = Client()
        cliente_2 = Client()

        cliente_1.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertTrue(cliente_1.session.get("_auth_user_id"))

        cliente_2.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertTrue(cliente_2.session.get("_auth_user_id"))

        # A primeira sessão deve ter sido encerrada na próxima requisição autenticada.
        resposta = cliente_1.get("/inicio/")
        self.assertFalse(cliente_1.session.get("_auth_user_id"))
        self.assertRedirects(resposta, "/login/")


class CadastroFuncionarioTestCase(MongoTesteBase):
    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True
        )
        self.comum = User.objects.create_user(username="15350946056", password=SENHA_FORTE)

    def _dados_funcionario(self, **overrides):
        dados = {
            "btn-salvar": "1",
            "NOME": "Joao da Silva",
            "DATA_NASCIMENTO": "1990-05-20",
            "RG": "123456789",
            "CPF": CPF_VALIDO,
            "SENHA": SENHA_FORTE,
            "NIVEL_ACESSO": "COMUM",
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    def test_supervisor_cadastra_funcionario_com_sucesso(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/cadastro-funcionario/", self._dados_funcionario())
        self.assertRedirects(resposta, "/cadastro-funcionario/")
        self.assertTrue(User.objects.filter(username=CPF_VALIDO).exists())
        self.assertIsNotNone(self.colecao_funcionarios.find_one({"CPF": CPF_VALIDO}))

    def test_funcionario_comum_nao_acessa_cadastro(self):
        self.client.force_login(self.comum)
        resposta = self.client.post("/cadastro-funcionario/", self._dados_funcionario())
        self.assertRedirects(resposta, "/inicio/")
        self.assertFalse(User.objects.filter(username=CPF_VALIDO).exists())

    def test_senha_fraca_e_rejeitada(self):
        self.client.force_login(self.supervisor)
        self.client.post("/cadastro-funcionario/", self._dados_funcionario(SENHA="123"))
        self.assertFalse(User.objects.filter(username=CPF_VALIDO).exists())

    def test_cpf_duplicado_e_rejeitado(self):
        self.client.force_login(self.supervisor)
        self.client.post("/cadastro-funcionario/", self._dados_funcionario())
        self.client.post(
            "/cadastro-funcionario/",
            self._dados_funcionario(NOME="Outra Pessoa"),
        )
        # Só deve existir 1 documento no Mongo e 1 usuário Django com esse CPF.
        self.assertEqual(self.colecao_funcionarios.count_documents({"CPF": CPF_VALIDO}), 1)
        self.assertEqual(User.objects.filter(username=CPF_VALIDO).count(), 1)


class CadastroObraTestCase(MongoTesteBase):
    def setUp(self):
        super().setUp()
        self.usuario = User.objects.create_user(username=CPF_VALIDO, password=SENHA_FORTE)
        self.client.force_login(self.usuario)

    def _dados_obra(self, **overrides):
        dados = {
            "btn-salvar": "1",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "1.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Teste",
            "ENDERECO": "Rua de Teste, 123",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    @patch("obras.views.cloudinary.uploader.upload")
    def _postar_cadastro(self, mock_upload, **overrides_dados):
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto.jpg"}
        from django.core.files.uploadedfile import SimpleUploadedFile

        dados = self._dados_obra(**overrides_dados)
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("foto.jpg", _JPEG_MINIMO, content_type="image/jpeg")}
        return self.client.post("/cadastro-obras/", {**dados, **arquivos})

    def test_cadastro_com_dados_validos_cria_obra(self):
        resposta = self._postar_cadastro()
        self.assertRedirects(resposta, "/cadastro-obras/")
        self.assertEqual(self.colecao_obras.count_documents({}), 1)
        obra = self.colecao_obras.find_one({})
        self.assertTrue(obra["ID_OBRA"].isdigit())

    def test_cnpj_invalido_e_rejeitado(self):
        resposta = self._postar_cadastro(CNPJ_EMPRESA="11222333000199")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)
        self.assertRedirects(resposta, "/cadastro-obras/")

    def test_data_inicio_depois_da_conclusao_e_rejeitada(self):
        resposta = self._postar_cadastro(DATA_INICIO="2026-12-31", CONCLUSAO_PREVISTA="2026-01-01")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)
        self.assertRedirects(resposta, "/cadastro-obras/")

    def test_campo_obrigatorio_faltando_e_rejeitado(self):
        resposta = self._postar_cadastro(NOME_EMPRESA="")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)
        self.assertRedirects(resposta, "/cadastro-obras/")

    def test_usuario_nao_logado_e_redirecionado_para_login(self):
        self.client.logout()
        resposta = self.client.post("/cadastro-obras/", self._dados_obra())
        self.assertTrue(resposta.url.startswith("/login/"))

    # ------------------------------------------------------------------
    # Validações de data
    # ------------------------------------------------------------------

    def test_data_inicio_igual_a_conclusao_e_rejeitada(self):
        resposta = self._postar_cadastro(DATA_INICIO="2026-06-01", CONCLUSAO_PREVISTA="2026-06-01")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_data_finalizacao_igual_a_inicio_e_rejeitada(self):
        resposta = self._postar_cadastro(
            DATA_INICIO="2026-01-01",
            CONCLUSAO_PREVISTA="2026-12-31",
            DATA_FINALIZACAO="2026-01-01",
        )
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_data_finalizacao_antes_do_inicio_e_rejeitada(self):
        resposta = self._postar_cadastro(
            DATA_INICIO="2026-06-01",
            CONCLUSAO_PREVISTA="2026-12-31",
            DATA_FINALIZACAO="2026-01-01",
        )
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_data_inicio_ano_absurdo_e_rejeitada(self):
        resposta = self._postar_cadastro(DATA_INICIO="2200-01-01", CONCLUSAO_PREVISTA="2200-12-31")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_data_finalizacao_opcional_ausente_e_aceita(self):
        """DATA_FINALIZACAO vazia é válida — obra ainda não foi concluída."""
        resposta = self._postar_cadastro(DATA_FINALIZACAO="")
        self.assertRedirects(resposta, "/cadastro-obras/")
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    # ------------------------------------------------------------------
    # Validações de campos de texto
    # ------------------------------------------------------------------

    def test_nome_empresa_so_numeros_e_rejeitado(self):
        resposta = self._postar_cadastro(NOME_EMPRESA="12345")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_endereco_so_numeros_e_rejeitado(self):
        resposta = self._postar_cadastro(ENDERECO="99999")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_tipo_obra_so_numeros_e_rejeitado(self):
        resposta = self._postar_cadastro(TIPO_OBRA="000")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    # ------------------------------------------------------------------
    # Validações de valor
    # ------------------------------------------------------------------

    def test_valor_zero_e_rejeitado(self):
        resposta = self._postar_cadastro(VALOR_OBRA="0,00")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_valor_nao_numerico_e_rejeitado(self):
        resposta = self._postar_cadastro(VALOR_OBRA="abc")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_valor_negativo_e_rejeitado(self):
        resposta = self._postar_cadastro(VALOR_OBRA="-1.000,00")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    # ------------------------------------------------------------------
    # Validações de situação / tipo de execução (allowlist)
    # ------------------------------------------------------------------

    def test_situacao_fora_da_allowlist_e_rejeitada(self):
        resposta = self._postar_cadastro(SITUACAO="Inventada")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_tipo_execucao_fora_da_allowlist_e_rejeitado(self):
        resposta = self._postar_cadastro(TIPO_EXECUCAO="Modo Fantasma")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    # ------------------------------------------------------------------
    # Campos obrigatórios em branco
    # ------------------------------------------------------------------

    def test_sem_tipo_obra_e_rejeitado(self):
        resposta = self._postar_cadastro(TIPO_OBRA="")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_sem_cnpj_e_rejeitado(self):
        resposta = self._postar_cadastro(CNPJ_EMPRESA="")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_sem_endereco_e_rejeitado(self):
        resposta = self._postar_cadastro(ENDERECO="")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_sem_data_inicio_e_rejeitado(self):
        resposta = self._postar_cadastro(DATA_INICIO="")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_sem_conclusao_prevista_e_rejeitado(self):
        resposta = self._postar_cadastro(CONCLUSAO_PREVISTA="")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    # ------------------------------------------------------------------
    # ID manual
    # ------------------------------------------------------------------

    def test_id_manual_com_letras_e_rejeitado(self):
        resposta = self._postar_cadastro(ID_OBRA_MANUAL="ABC")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    @patch("obras.views.cloudinary.uploader.upload")
    def test_id_manual_numerico_e_aceito(self, mock_upload):
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto.jpg"}
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = self._dados_obra(ID_OBRA_MANUAL="9999")
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("foto.jpg", _JPEG_MINIMO, content_type="image/jpeg")}
        self.client.post("/cadastro-obras/", {**dados, **arquivos})
        obra = self.colecao_obras.find_one({"ID_OBRA": "9999"})
        self.assertIsNotNone(obra)


class CadastroFuncionarioValidacaoTestCase(MongoTesteBase):
    """Testa campo a campo as validações do cadastro de funcionário."""

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor Teste"
        )
        self.client.force_login(self.supervisor)

    def _dados_func(self, **overrides):
        dados = {
            "btn-salvar": "1",
            "NOME": "Carlos da Silva",
            "DATA_NASCIMENTO": "1990-05-20",
            "RG": "123456789",
            "CPF": CPF_VALIDO,
            "SENHA": SENHA_FORTE,
            "NIVEL_ACESSO": "COMUM",
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    def _postar(self, **overrides):
        return self.client.post("/cadastro-funcionario/", self._dados_func(**overrides))

    def _funcionario_criado(self):
        return User.objects.filter(username=CPF_VALIDO).exists()

    # ------------------------------------------------------------------
    # Campos obrigatórios em branco
    # ------------------------------------------------------------------

    def test_sem_nome_e_rejeitado(self):
        self._postar(NOME="")
        self.assertFalse(self._funcionario_criado())

    def test_sem_cpf_e_rejeitado(self):
        self._postar(CPF="")
        self.assertFalse(self._funcionario_criado())

    def test_sem_rg_e_rejeitado(self):
        self._postar(RG="")
        self.assertFalse(self._funcionario_criado())

    def test_sem_data_nascimento_e_rejeitado(self):
        self._postar(DATA_NASCIMENTO="")
        self.assertFalse(self._funcionario_criado())

    def test_sem_senha_e_rejeitado(self):
        self._postar(SENHA="")
        self.assertFalse(self._funcionario_criado())

    def test_sem_nivel_acesso_e_rejeitado(self):
        self._postar(NIVEL_ACESSO="")
        self.assertFalse(self._funcionario_criado())

    # ------------------------------------------------------------------
    # Validação de CPF
    # ------------------------------------------------------------------

    def test_cpf_com_digito_verificador_errado_e_rejeitado(self):
        self._postar(CPF="11144477736")
        self.assertFalse(self._funcionario_criado())

    def test_cpf_com_todos_digitos_iguais_e_rejeitado(self):
        self._postar(CPF="11111111111")
        self.assertFalse(self._funcionario_criado())

    def test_cpf_curto_demais_e_rejeitado(self):
        self._postar(CPF="123")
        self.assertFalse(self._funcionario_criado())

    # ------------------------------------------------------------------
    # Validação de RG
    # ------------------------------------------------------------------

    def test_rg_com_letra_e_rejeitado(self):
        self._postar(RG="1234567X")
        self.assertFalse(self._funcionario_criado())

    # ------------------------------------------------------------------
    # Validação de nome
    # ------------------------------------------------------------------

    def test_nome_so_numeros_e_rejeitado(self):
        self._postar(NOME="12345678")
        self.assertFalse(self._funcionario_criado())

    # ------------------------------------------------------------------
    # Validação de data de nascimento
    # ------------------------------------------------------------------

    def test_data_nascimento_futura_e_rejeitada(self):
        self._postar(DATA_NASCIMENTO="2099-01-01")
        self.assertFalse(self._funcionario_criado())

    def test_supervisor_menor_de_18_anos_e_rejeitado(self):
        from datetime import date
        hoje = date.today()
        try:
            menor = hoje.replace(year=hoje.year - 17).strftime("%Y-%m-%d")
        except ValueError:
            menor = hoje.replace(year=hoje.year - 17, day=28).strftime("%Y-%m-%d")
        self._postar(NIVEL_ACESSO="SUPERVISOR", DATA_NASCIMENTO=menor)
        self.assertFalse(self._funcionario_criado())

    def test_funcionario_comum_menor_de_16_anos_e_rejeitado(self):
        from datetime import date
        hoje = date.today()
        try:
            menor = hoje.replace(year=hoje.year - 15).strftime("%Y-%m-%d")
        except ValueError:
            menor = hoje.replace(year=hoje.year - 15, day=28).strftime("%Y-%m-%d")
        self._postar(NIVEL_ACESSO="COMUM", DATA_NASCIMENTO=menor)
        self.assertFalse(self._funcionario_criado())

    def test_funcionario_comum_com_16_anos_e_aceito(self):
        from datetime import date
        hoje = date.today()
        dezesseis = date(hoje.year - 16, hoje.month, hoje.day).strftime("%Y-%m-%d")
        self._postar(NIVEL_ACESSO="COMUM", DATA_NASCIMENTO=dezesseis)
        self.assertTrue(self._funcionario_criado())

    # ------------------------------------------------------------------
    # Validação de nível de acesso
    # ------------------------------------------------------------------

    def test_nivel_acesso_invalido_e_rejeitado(self):
        self._postar(NIVEL_ACESSO="GERENTE")
        self.assertFalse(self._funcionario_criado())

    # ------------------------------------------------------------------
    # Validação de senha
    # ------------------------------------------------------------------

    def test_senha_muito_curta_e_rejeitada(self):
        self._postar(SENHA="Ab1!")
        self.assertFalse(self._funcionario_criado())

    def test_senha_forte_e_aceita(self):
        """Senha que passa todos os validators (comprimento, complexidade, não-numérica) deve ser aceita."""
        self._postar(SENHA=SENHA_FORTE)
        self.assertTrue(self._funcionario_criado())

    def test_senha_so_numeros_e_aceita(self):
        """Senha somente numérica com 8+ chars deve ser aceita — validate_password() foi
        removido por design: admins definem senhas, não o próprio usuário."""
        self._postar(SENHA="12345678")
        self.assertTrue(self._funcionario_criado())


class EdicaoFuncionarioValidacaoTestCase(MongoTesteBase):
    """Testa campo a campo as validações de salva_edicao_funcionario."""

    CPF_FUNCIONARIO = CPF_VALIDO

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor Teste"
        )
        # Cria o funcionário alvo no Django e no Mongo
        User.objects.create_user(
            username=self.CPF_FUNCIONARIO, password=SENHA_FORTE, first_name="Carlos Silva"
        )
        self.colecao_funcionarios.insert_one({
            "CPF": self.CPF_FUNCIONARIO,
            "NOME": "CARLOS SILVA",
            "DATA_NASCIMENTO": "20/05/1990",
            "RG": "123456789",
            "FUNCAO": "COMUM",
        })
        self.client.force_login(self.supervisor)
        # Simula o fluxo de busca: a sessão precisa ter cpf_editando definido
        session = self.client.session
        session["cpf_editando"] = self.CPF_FUNCIONARIO
        session.save()

    def _dados_edicao(self, **overrides):
        dados = {
            "CPF_ORIGINAL": self.CPF_FUNCIONARIO,
            "NOME": "Carlos da Silva Editado",
            "DATA_NASCIMENTO": "1990-05-20",
            "RG": "123456789",
            "FUNCAO": "COMUM",
            "SENHA": "",
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    def _postar(self, **overrides):
        return self.client.post("/salvar-edicao-funcionario/", self._dados_edicao(**overrides))

    def _funcionario_atualizado(self, nome_esperado):
        doc = self.colecao_funcionarios.find_one({"CPF": self.CPF_FUNCIONARIO})
        return doc and doc.get("NOME") == nome_esperado.upper()

    # ------------------------------------------------------------------
    # Campos obrigatórios
    # ------------------------------------------------------------------

    def test_nome_em_branco_e_rejeitado(self):
        self._postar(NOME="")
        self.assertFalse(self._funcionario_atualizado(""))

    def test_rg_em_branco_e_rejeitado(self):
        self._postar(RG="")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_data_nascimento_em_branco_e_rejeitada(self):
        self._postar(DATA_NASCIMENTO="")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_funcao_em_branco_e_rejeitada(self):
        self._postar(FUNCAO="")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    # ------------------------------------------------------------------
    # Validação de data de nascimento
    # ------------------------------------------------------------------

    def test_data_nascimento_futura_e_rejeitada(self):
        self._postar(DATA_NASCIMENTO="2099-01-01")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_supervisor_menor_de_18_anos_e_rejeitado(self):
        from datetime import date
        hoje = date.today()
        try:
            menor = hoje.replace(year=hoje.year - 17).strftime("%Y-%m-%d")
        except ValueError:
            menor = hoje.replace(year=hoje.year - 17, day=28).strftime("%Y-%m-%d")
        self._postar(FUNCAO="SUPERVISOR", DATA_NASCIMENTO=menor)
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_comum_menor_de_16_anos_e_rejeitado(self):
        from datetime import date
        hoje = date.today()
        try:
            menor = hoje.replace(year=hoje.year - 15).strftime("%Y-%m-%d")
        except ValueError:
            menor = hoje.replace(year=hoje.year - 15, day=28).strftime("%Y-%m-%d")
        self._postar(FUNCAO="COMUM", DATA_NASCIMENTO=menor)
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    # ------------------------------------------------------------------
    # Validação de RG e nome
    # ------------------------------------------------------------------

    def test_rg_com_letra_e_rejeitado(self):
        self._postar(RG="1234567X")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_nome_so_numeros_e_rejeitado(self):
        self._postar(NOME="99999999")
        self.assertFalse(self._funcionario_atualizado("99999999"))

    # ------------------------------------------------------------------
    # Validação de função
    # ------------------------------------------------------------------

    def test_funcao_invalida_e_rejeitada(self):
        self._postar(FUNCAO="GERENTE")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    # ------------------------------------------------------------------
    # Validação de senha (só quando preenchida)
    # ------------------------------------------------------------------

    def test_nova_senha_fraca_e_rejeitada(self):
        self._postar(SENHA="123")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_senha_vazia_mantem_a_atual_e_aceita(self):
        self._postar(SENHA="")
        self.assertTrue(self._funcionario_atualizado("Carlos da Silva Editado"))

    # ------------------------------------------------------------------
    # Guard de sessão
    # ------------------------------------------------------------------

    def test_cpf_original_diferente_da_sessao_e_rejeitado(self):
        self._postar(CPF_ORIGINAL="99999999999")
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_sem_sessao_cpf_editando_e_rejeitado(self):
        session = self.client.session
        session.pop("cpf_editando", None)
        session.save()
        self._postar()
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))


class EdicaoObraValidacaoTestCase(MongoTesteBase):
    """Testa campo a campo as validações de salva_edicao_obra."""

    ID_OBRA_TESTE = "12026"

    def setUp(self):
        super().setUp()
        self.usuario = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )
        self.client.force_login(self.usuario)
        # Insere a obra alvo no banco de teste
        self.colecao_obras.insert_one({
            "ID_OBRA": self.ID_OBRA_TESTE,
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "VALOR_OBRA": 1000.0,
            "DATA_INICIO": "01/01/2026",
            "CONCLUSAO_PREVISTA": "31/12/2026",
            "DATA_FINALIZACAO": "—",
            "NOME_EMPRESA": "Empresa Teste",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "EMPRESA_CONTRATADA": "EMPRESA TESTE - CNPJ - " + CNPJ_VALIDO,
            "TIPO_EXECUCAO": "Nova Construção",
            "ENDERECO": "Rua de Teste, 123",
        })

    def _dados_edicao(self, **overrides):
        dados = {
            "ID_OBRA": self.ID_OBRA_TESTE,
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "2.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Editada",
            "ENDERECO": "Rua Editada, 456",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    def _postar(self, **overrides):
        return self.client.post("/salvar-edicao-obra/", self._dados_edicao(**overrides))

    def _obra_atualizada(self):
        doc = self.colecao_obras.find_one({"ID_OBRA": self.ID_OBRA_TESTE})
        return doc and doc.get("NOME_EMPRESA") == "Empresa Editada"

    # ------------------------------------------------------------------
    # Campos obrigatórios
    # ------------------------------------------------------------------

    def test_sem_tipo_obra_e_rejeitado(self):
        self._postar(TIPO_OBRA="")
        self.assertFalse(self._obra_atualizada())

    def test_sem_nome_empresa_e_rejeitado(self):
        self._postar(NOME_EMPRESA="")
        self.assertFalse(self._obra_atualizada())

    def test_sem_cnpj_e_rejeitado(self):
        self._postar(CNPJ_EMPRESA="")
        self.assertFalse(self._obra_atualizada())

    def test_sem_endereco_e_rejeitado(self):
        self._postar(ENDERECO="")
        self.assertFalse(self._obra_atualizada())

    def test_sem_data_inicio_e_rejeitado(self):
        self._postar(DATA_INICIO="")
        self.assertFalse(self._obra_atualizada())

    def test_sem_conclusao_prevista_e_rejeitado(self):
        self._postar(CONCLUSAO_PREVISTA="")
        self.assertFalse(self._obra_atualizada())

    def test_sem_valor_e_rejeitado(self):
        self._postar(VALOR_OBRA="")
        self.assertFalse(self._obra_atualizada())

    # ------------------------------------------------------------------
    # Validação de datas
    # ------------------------------------------------------------------

    def test_data_inicio_depois_da_conclusao_e_rejeitada(self):
        self._postar(DATA_INICIO="2026-12-31", CONCLUSAO_PREVISTA="2026-01-01")
        self.assertFalse(self._obra_atualizada())

    def test_data_inicio_igual_a_conclusao_e_rejeitada(self):
        self._postar(DATA_INICIO="2026-06-01", CONCLUSAO_PREVISTA="2026-06-01")
        self.assertFalse(self._obra_atualizada())

    def test_data_finalizacao_antes_do_inicio_e_rejeitada(self):
        self._postar(DATA_INICIO="2026-06-01", CONCLUSAO_PREVISTA="2026-12-31", DATA_FINALIZACAO="2026-01-01")
        self.assertFalse(self._obra_atualizada())

    def test_data_finalizacao_igual_ao_inicio_e_rejeitada(self):
        self._postar(DATA_INICIO="2026-06-01", CONCLUSAO_PREVISTA="2026-12-31", DATA_FINALIZACAO="2026-06-01")
        self.assertFalse(self._obra_atualizada())

    def test_data_inicio_ano_absurdo_e_rejeitada(self):
        self._postar(DATA_INICIO="2200-01-01", CONCLUSAO_PREVISTA="2200-12-31")
        self.assertFalse(self._obra_atualizada())

    def test_data_finalizacao_vazia_e_aceita(self):
        self._postar(DATA_FINALIZACAO="")
        self.assertTrue(self._obra_atualizada())

    # ------------------------------------------------------------------
    # Validação de CNPJ
    # ------------------------------------------------------------------

    def test_cnpj_invalido_e_rejeitado(self):
        self._postar(CNPJ_EMPRESA="11222333000199")
        self.assertFalse(self._obra_atualizada())

    # ------------------------------------------------------------------
    # Validação de valor
    # ------------------------------------------------------------------

    def test_valor_zero_e_rejeitado(self):
        self._postar(VALOR_OBRA="0,00")
        self.assertFalse(self._obra_atualizada())

    def test_valor_nao_numerico_e_rejeitado(self):
        self._postar(VALOR_OBRA="abc")
        self.assertFalse(self._obra_atualizada())

    # ------------------------------------------------------------------
    # Validação de campos de texto
    # ------------------------------------------------------------------

    def test_nome_empresa_so_numeros_e_rejeitado(self):
        self._postar(NOME_EMPRESA="12345")
        self.assertFalse(self._obra_atualizada())

    def test_endereco_so_numeros_e_rejeitado(self):
        self._postar(ENDERECO="99999")
        self.assertFalse(self._obra_atualizada())

    # ------------------------------------------------------------------
    # Validação de allowlist (situação / tipo de execução)
    # ------------------------------------------------------------------

    def test_situacao_invalida_e_rejeitada(self):
        self._postar(SITUACAO="Inventada")
        self.assertFalse(self._obra_atualizada())

    def test_tipo_execucao_invalido_e_rejeitado(self):
        self._postar(TIPO_EXECUCAO="Modo Fantasma")
        self.assertFalse(self._obra_atualizada())

    # ------------------------------------------------------------------
    # Obra inexistente
    # ------------------------------------------------------------------

    def test_id_obra_inexistente_e_rejeitado(self):
        self._postar(ID_OBRA="99999")
        self.assertFalse(self._obra_atualizada())


# ==============================================================================
# LOGIN / LOGOUT
# ==============================================================================

class LoginViewTestCase(MongoTesteBase):
    """Cobre todos os comportamentos da login_view além do rate-limit."""

    def setUp(self):
        super().setUp()
        self.cpf = CPF_VALIDO
        self.user = User.objects.create_user(
            username=self.cpf, password=SENHA_FORTE, first_name="Joao Teste"
        )

    # ------------------------------------------------------------------
    # Fluxo feliz
    # ------------------------------------------------------------------

    def test_login_correto_exibe_modal_sucesso(self):
        # Login bem-sucedido renderiza modal de confirmação (200) em vez de redirecionar.
        resposta = self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.context.get("login_success"))

    def test_login_correto_autentica_sessao(self):
        self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertTrue(self.client.session.get("_auth_user_id"))

    def test_login_com_cpf_formatado_pontos_tracos_funciona(self):
        """CPF com formatação visual deve ser limpo e autenticar normalmente."""
        cpf_formatado = "111.444.777-35"
        resposta = self.client.post("/login/", {"username": cpf_formatado, "password": SENHA_FORTE})
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.context.get("login_success"))

    def test_login_correto_next_seguro_incluso_no_contexto(self):
        """?next= seguro deve aparecer como proxima_url no contexto do modal."""
        resposta = self.client.post(
            "/login/?next=/inicio/",
            {"username": self.cpf, "password": SENHA_FORTE},
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.context.get("proxima_url"), "/inicio/")

    def test_login_next_externo_usa_inicio_como_fallback(self):
        """Open redirect: ?next= apontando para domínio externo deve ser ignorado."""
        resposta = self.client.post(
            "/login/?next=http://malicioso.com",
            {"username": self.cpf, "password": SENHA_FORTE},
        )
        self.assertEqual(resposta.status_code, 200)
        # proxima_url deve ser o fallback seguro, nunca a URL externa.
        self.assertNotIn("malicioso.com", resposta.context.get("proxima_url", ""))

    # ------------------------------------------------------------------
    # Credenciais erradas
    # ------------------------------------------------------------------

    def test_senha_errada_nao_autentica(self):
        self.client.post("/login/", {"username": self.cpf, "password": "SenhaErrada!1"})
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_cpf_inexistente_nao_autentica(self):
        self.client.post("/login/", {"username": "00000000000", "password": SENHA_FORTE})
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_login_sem_cpf_nao_autentica(self):
        self.client.post("/login/", {"username": "", "password": SENHA_FORTE})
        self.assertFalse(self.client.session.get("_auth_user_id"))

    # ------------------------------------------------------------------
    # GET — página de login
    # ------------------------------------------------------------------

    def test_get_login_retorna_200(self):
        resposta = self.client.get("/login/")
        self.assertEqual(resposta.status_code, 200)

    def test_usuario_ja_logado_e_redirecionado_para_inicio(self):
        """Usuário autenticado que acessa GET /login/ é redirecionado para /inicio/."""
        self.client.force_login(self.user)
        resposta = self.client.get("/login/")
        self.assertRedirects(resposta, '/inicio/')

    def test_usuario_ja_logado_post_login_e_redirecionado(self):
        """POST para /login/ de usuário já autenticado redireciona para /inicio/ sem processar."""
        self.client.force_login(self.user)
        resposta = self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertRedirects(resposta, '/inicio/')

    # ------------------------------------------------------------------
    # Conta superuser sem first_name (bug corrigido — IndexError)
    # ------------------------------------------------------------------

    def test_superuser_sem_first_name_faz_login_sem_erro(self):
        su = User.objects.create_superuser(username="00000000191", password=SENHA_FORTE)
        su.first_name = ""
        su.save()
        resposta = self.client.post("/login/", {"username": "00000000191", "password": SENHA_FORTE})
        # Deve retornar 200 (modal de sucesso), não 500 por IndexError no split().
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.context.get("login_success"))


class LogoutViewTestCase(MongoTesteBase):
    """Cobre logout_view."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )
        self.client.force_login(self.user)

    def test_logout_post_encerra_sessao(self):
        self.client.post("/logout/")
        self.assertFalse(self.client.session.get("_auth_user_id"))

    def test_logout_redireciona_para_login(self):
        resposta = self.client.post("/logout/")
        self.assertIn("/login/", resposta.url)

    def test_logout_por_inatividade_redireciona_para_login_com_flag_sessao(self):
        resposta = self.client.post("/logout/", {"motivo": "inatividade"})
        self.assertIn("/login/", resposta.url)
        self.assertEqual(self.client.session.get('aviso_login'), 'inatividade')

    def test_logout_get_retorna_405(self):
        resposta = self.client.get("/logout/")
        self.assertEqual(resposta.status_code, 405)

    def test_logout_sem_autenticacao_nao_quebra(self):
        self.client.logout()
        resposta = self.client.post("/logout/")
        self.assertIn("/login/", resposta.url)


# ==============================================================================
# PÁGINAS PÚBLICAS
# ==============================================================================

class ListaObrasPublicaTestCase(MongoTesteBase):
    """lista_obras é pública — não exige autenticação."""

    def test_acesso_sem_login_retorna_200(self):
        resposta = self.client.get("/lista-obras/")
        self.assertEqual(resposta.status_code, 200)

    def test_lista_vazia_retorna_200(self):
        resposta = self.client.get("/lista-obras/")
        self.assertEqual(resposta.status_code, 200)

    def test_lista_com_obras_exibe_resultado(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()  # garante que o cache da lista vazia não interfira
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "VALOR_OBRA": 1000.0,
            "NOME_EMPRESA": "Empresa Teste",
            "ENDERECO": "Rua A",
            "URL_FOTO": "",
            "TIMESTAMP_CADASTRO": __import__("datetime").datetime.now(),
        })
        resposta = self.client.get("/lista-obras/")
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "12026")

    def test_paginacao_pagina_invalida_usa_pagina_1(self):
        resposta = self.client.get("/lista-obras/?pagina=abc")
        self.assertEqual(resposta.status_code, 200)

    def test_paginacao_pagina_negativa_usa_pagina_1(self):
        resposta = self.client.get("/lista-obras/?pagina=-5")
        self.assertEqual(resposta.status_code, 200)

    def test_cache_e_invalidado_apos_nova_obra(self):
        """_bump_cache_obras() deve fazer obras novas aparecerem imediatamente."""
        from django.core.cache import cache as django_cache
        from obras.views import _bump_cache_obras
        import datetime
        django_cache.clear()

        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "VALOR_OBRA": 1000.0,
            "NOME_EMPRESA": "Empresa A",
            "ENDERECO": "Rua A",
            "URL_FOTO": "",
            "TIMESTAMP_CADASTRO": datetime.datetime.now(),
        })
        # Primeira visita popula o cache com ID 12026
        resposta1 = self.client.get("/lista-obras/")
        self.assertContains(resposta1, "12026")

        # Insere nova obra SEM bumpar o cache — não deve aparecer ainda
        self.colecao_obras.insert_one({
            "ID_OBRA": "22026",
            "TIPO_OBRA": "CALÇADA",
            "SITUACAO": "Em andamento",
            "VALOR_OBRA": 500.0,
            "NOME_EMPRESA": "Empresa B",
            "ENDERECO": "Rua B",
            "URL_FOTO": "",
            "TIMESTAMP_CADASTRO": datetime.datetime.now(),
        })
        resposta_cache = self.client.get("/lista-obras/")
        self.assertNotContains(resposta_cache, "22026")

        # Bumpa o cache — obra nova deve aparecer na próxima requisição
        _bump_cache_obras()
        resposta_apos_bump = self.client.get("/lista-obras/")
        self.assertContains(resposta_apos_bump, "22026")


class GaleriaObraPublicaTestCase(MongoTesteBase):
    """galeria_obra é pública — não exige autenticação."""

    def setUp(self):
        super().setUp()
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "URL_FOTO": "https://res.cloudinary.com/teste/foto.jpg",
            "GALERIA": ["https://res.cloudinary.com/teste/foto.jpg"],
            "DATA_CADASTRO": "01/01/2026 00:00:00",
        })

    def test_galeria_obra_existente_retorna_200(self):
        resposta = self.client.get("/galeria/12026/")
        self.assertEqual(resposta.status_code, 200)

    def test_galeria_obra_inexistente_retorna_200_vazio(self):
        """Obra não encontrada deve retornar 200 com galeria vazia, não 500."""
        resposta = self.client.get("/galeria/99999/")
        self.assertEqual(resposta.status_code, 200)

    def test_galeria_sem_login_retorna_200(self):
        resposta = self.client.get("/galeria/12026/")
        self.assertEqual(resposta.status_code, 200)

    def test_galeria_com_timelapse_exibe_datas(self):
        """mapa_datas deve cruzar URLs de galeria com datas do timelapse."""
        from datetime import datetime
        url_foto = "https://res.cloudinary.com/teste/foto.jpg"
        self.colecao_timelapse.insert_one({
            "ID_OBRA": "12026",
            "URL_FOTO": url_foto,
            "DATA_REGISTRO": "01/01/2026",
            "TIMESTAMP": datetime(2026, 1, 1),
        })
        resposta = self.client.get("/galeria/12026/")
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "01/01/2026")


# ==============================================================================
# PÁGINAS INTERNAS — ACESSO SEM AUTENTICAÇÃO
# ==============================================================================

class AcessoNaoAutenticadoTestCase(MongoTesteBase):
    """Garante que todas as views protegidas redirecionam para login."""

    def test_index_retorna_200_sem_login(self):
        """/inicio/ renderiza normalmente sem autenticação — controle de acesso é no template."""
        resposta = self.client.get("/inicio/")
        self.assertEqual(resposta.status_code, 200)

    def test_cadastro_obras_redireciona_para_login(self):
        resposta = self.client.get("/cadastro-obras/")
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_cadastro_funcionario_redireciona_para_login(self):
        resposta = self.client.get("/cadastro-funcionario/")
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_busca_obra_redireciona_para_login(self):
        resposta = self.client.get("/atualizar-obra/")
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_salvar_edicao_obra_redireciona_para_login(self):
        resposta = self.client.post("/salvar-edicao-obra/", {})
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_busca_funcionario_redireciona_para_login(self):
        resposta = self.client.get("/atualizar-funcionario/")
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_salvar_edicao_funcionario_redireciona_para_login(self):
        resposta = self.client.post("/salvar-edicao-funcionario/", {})
        self.assertTrue(resposta.url.startswith("/login/"))


# ==============================================================================
# BUSCA DE FUNCIONÁRIO
# ==============================================================================

class BuscaFuncionarioTestCase(MongoTesteBase):
    """Cobre busca_atualiza_funcionario."""

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor"
        )
        self.comum = User.objects.create_user(
            username="15350946056", password=SENHA_FORTE, first_name="Comum"
        )
        self.colecao_funcionarios.insert_one({
            "CPF": CPF_VALIDO,
            "NOME": "JOAO DA SILVA",
            "DATA_NASCIMENTO": "20/05/1990",
            "RG": "123456789",
            "FUNCAO": "COMUM",
        })
        self.client.force_login(self.supervisor)

    def test_busca_cpf_encontrado_retorna_formulario_edicao(self):
        resposta = self.client.post(
            "/atualizar-funcionario/",
            {"CPF": CPF_VALIDO},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "JOAO DA SILVA")

    def test_busca_cpf_nao_encontrado_exibe_erro(self):
        resposta = self.client.post(
            "/atualizar-funcionario/",
            {"CPF": "00000000191"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)

    def test_busca_cpf_invalido_exibe_erro(self):
        resposta = self.client.post(
            "/atualizar-funcionario/",
            {"CPF": "abc"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)

    def test_busca_cpf_vazio_exibe_erro(self):
        resposta = self.client.post(
            "/atualizar-funcionario/",
            {"CPF": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)

    def test_funcionario_comum_nao_acessa_busca(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/atualizar-funcionario/")
        self.assertRedirects(resposta, "/inicio/")

    def test_get_retorna_200(self):
        resposta = self.client.get("/atualizar-funcionario/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_busca_define_cpf_editando_na_sessao(self):
        self.client.post("/atualizar-funcionario/", {"CPF": CPF_VALIDO})
        self.assertEqual(self.client.session.get("cpf_editando"), CPF_VALIDO)


# ==============================================================================
# BUSCA DE OBRA
# ==============================================================================

class BuscaObraTestCase(MongoTesteBase):
    """Cobre busca_atualiza_obra."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao"
        )
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "VALOR_OBRA": 1000.0,
            "DATA_INICIO": "01/01/2026",
            "CONCLUSAO_PREVISTA": "31/12/2026",
            "DATA_FINALIZACAO": "—",
            "NOME_EMPRESA": "Empresa Teste",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "TIPO_EXECUCAO": "Nova Construção",
            "ENDERECO": "Rua de Teste, 123",
        })
        self.client.force_login(self.user)

    def test_busca_obra_existente_retorna_formulario_edicao(self):
        resposta = self.client.post(
            "/atualizar-obra/",
            {"ID_OBRA": "12026"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "12026")

    def test_busca_obra_inexistente_exibe_erro(self):
        resposta = self.client.post(
            "/atualizar-obra/",
            {"ID_OBRA": "99999"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)

    def test_get_retorna_200(self):
        resposta = self.client.get("/atualizar-obra/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_post_com_id_vazio_retorna_erro_sem_query(self):
        """POST com ID_OBRA vazio deve retornar erro sem ir ao MongoDB."""
        resposta = self.client.post(
            "/atualizar-obra/",
            {"ID_OBRA": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)
        # Nenhuma obra deve ter sido modificada
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    def test_post_com_id_espacos_retorna_erro(self):
        resposta = self.client.post(
            "/atualizar-obra/",
            {"ID_OBRA": "   "},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)


# ==============================================================================
# TOKEN DE IDEMPOTÊNCIA
# ==============================================================================

class TokenIdempotenciaTestCase(MongoTesteBase):
    """Garante que o token de idempotência bloqueia resubmissões."""

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor"
        )
        self.client.force_login(self.supervisor)

    def test_token_ausente_em_cadastro_funcionario_e_rejeitado(self):
        dados = {
            "btn-salvar": "1",
            "NOME": "Carlos Silva",
            "DATA_NASCIMENTO": "1990-05-20",
            "RG": "123456789",
            "CPF": CPF_VALIDO,
            "SENHA": SENHA_FORTE,
            "NIVEL_ACESSO": "COMUM",
            # form_token ausente
        }
        self.client.post("/cadastro-funcionario/", dados)
        self.assertFalse(User.objects.filter(username=CPF_VALIDO).exists())

    def test_mesmo_token_usado_duas_vezes_em_cadastro_funcionario_cria_apenas_um(self):
        CPF_SEGUNDA_TENTATIVA = "00000000191"  # CPF válido, diferente do supervisor e do CPF_VALIDO
        token = _gerar_form_token()
        dados = {
            "btn-salvar": "1",
            "NOME": "Carlos Silva",
            "DATA_NASCIMENTO": "1990-05-20",
            "RG": "123456789",
            "CPF": CPF_VALIDO,
            "SENHA": SENHA_FORTE,
            "NIVEL_ACESSO": "COMUM",
            "form_token": token,
        }
        self.client.post("/cadastro-funcionario/", dados)
        # Segunda submissão com o mesmo token mas CPF diferente
        dados2 = dict(dados)
        dados2["CPF"] = CPF_SEGUNDA_TENTATIVA
        dados2["form_token"] = token
        self.client.post("/cadastro-funcionario/", dados2)
        # Só o primeiro deve ter sido criado; o segundo foi bloqueado pelo token repetido
        self.assertTrue(User.objects.filter(username=CPF_VALIDO).exists())
        self.assertFalse(User.objects.filter(username=CPF_SEGUNDA_TENTATIVA).exists())


# ==============================================================================
# INDEX / PÁGINAS INTERNAS COM LOGIN
# ==============================================================================

class PaginasInternasTestCase(MongoTesteBase):
    """Verifica que páginas internas renderizam corretamente para usuários autenticados."""

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor Teste"
        )
        self.comum = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Comum Teste"
        )

    def test_index_supervisor_retorna_200(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/inicio/")
        self.assertEqual(resposta.status_code, 200)

    def test_index_comum_retorna_200(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/inicio/")
        self.assertEqual(resposta.status_code, 200)

    def test_cadastro_obras_get_retorna_200(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/cadastro-obras/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_cadastro_funcionario_get_supervisor_retorna_200(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/cadastro-funcionario/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_cadastro_funcionario_get_comum_redireciona(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/cadastro-funcionario/")
        self.assertRedirects(resposta, "/inicio/")

    def test_busca_obra_get_retorna_200(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/atualizar-obra/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_busca_funcionario_get_supervisor_retorna_200(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/atualizar-funcionario/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)


# ==============================================================================
# ROTA RAIZ
# ==============================================================================

class PaginaInicialRaizTestCase(MongoTesteBase):
    """Cobre pagina_inicial (/) — rota raiz separada de /inicio/."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )

    def test_raiz_sem_login_retorna_200(self):
        resposta = self.client.get("/")
        self.assertEqual(resposta.status_code, 200)

    def test_raiz_com_login_retorna_200(self):
        self.client.force_login(self.user)
        resposta = self.client.get("/")
        self.assertEqual(resposta.status_code, 200)

    def test_raiz_htmx_retorna_parcial(self):
        self.client.force_login(self.user)
        resposta = self.client.get("/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)


# ==============================================================================
# VALIDAÇÃO DE UPLOAD DE FOTO
# ==============================================================================

# Cabeçalhos mínimos para cada formato suportado
_PNG_MINIMO = b'\x89PNG\r\n\x1a\n' + b'\x00' * 4
_GIF_MINIMO = b'GIF89a' + b'\x00' * 10
_WEBP_MINIMO = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 4
# Arquivo que parece JPEG pela extensão mas tem conteúdo binário aleatório
_CONTEUDO_INVALIDO = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c'


class ValidacaoFotoUploadTestCase(MongoTesteBase):
    """Cobre _validar_foto via cadastro_obras — extensão, magic bytes e tamanho."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )
        self.client.force_login(self.user)

    def _dados_obra(self, **overrides):
        dados = {
            "btn-salvar": "1",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "1.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Teste",
            "ENDERECO": "Rua de Teste, 123",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    @patch("obras.views.cloudinary.uploader.upload")
    def _postar_com_foto(self, mock_upload, nome_arquivo, conteudo, content_type="image/jpeg"):
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto.jpg"}
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = self._dados_obra()
        arquivos = {"FOTO_OBRA": SimpleUploadedFile(nome_arquivo, conteudo, content_type=content_type)}
        return self.client.post("/cadastro-obras/", {**dados, **arquivos})

    def test_jpeg_valido_e_aceito(self):
        self._postar_com_foto(nome_arquivo="foto.jpg", conteudo=_JPEG_MINIMO)
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    def test_png_valido_e_aceito(self):
        self._postar_com_foto(nome_arquivo="foto.png", conteudo=_PNG_MINIMO, content_type="image/png")
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    def test_gif_valido_e_aceito(self):
        self._postar_com_foto(nome_arquivo="foto.gif", conteudo=_GIF_MINIMO, content_type="image/gif")
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    def test_extensao_invalida_e_rejeitada(self):
        self._postar_com_foto(nome_arquivo="arquivo.exe", conteudo=_JPEG_MINIMO, content_type="application/octet-stream")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_extensao_pdf_e_rejeitada(self):
        self._postar_com_foto(nome_arquivo="arquivo.pdf", conteudo=b'%PDF-1.4', content_type="application/pdf")
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_magic_bytes_invalidos_rejeitam_mesmo_com_extensao_jpg(self):
        """Extensão .jpg com conteúdo binário inválido deve ser rejeitado."""
        self._postar_com_foto(nome_arquivo="falso.jpg", conteudo=_CONTEUDO_INVALIDO)
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_sem_foto_e_rejeitado(self):
        dados = self._dados_obra()
        resposta = self.client.post("/cadastro-obras/", dados)
        self.assertEqual(self.colecao_obras.count_documents({}), 0)

    def test_mais_de_10_fotos_e_rejeitado(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = self._dados_obra()
        fotos = [
            SimpleUploadedFile(f"foto{i}.jpg", _JPEG_MINIMO, content_type="image/jpeg")
            for i in range(11)
        ]
        self.client.post("/cadastro-obras/", {**dados, "FOTO_OBRA": fotos})
        self.assertEqual(self.colecao_obras.count_documents({}), 0)


# ==============================================================================
# TOKEN DE IDEMPOTÊNCIA — FORMULÁRIOS DE OBRA
# ==============================================================================

class TokenIdempotenciaObraTestCase(MongoTesteBase):
    """Token de idempotência para cadastro_obras e salva_edicao_obra."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )
        self.client.force_login(self.user)
        # Obra pré-existente para testes de edição
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": 1000.0,
            "DATA_INICIO": "01/01/2026",
            "CONCLUSAO_PREVISTA": "31/12/2026",
            "DATA_FINALIZACAO": "—",
            "NOME_EMPRESA": "Empresa Original",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "EMPRESA_CONTRATADA": "EMPRESA ORIGINAL - CNPJ - " + CNPJ_VALIDO,
            "ENDERECO": "Rua Original, 1",
        })

    @patch("obras.views.cloudinary.uploader.upload")
    def test_token_ausente_em_cadastro_obras_e_rejeitado(self, mock_upload):
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto.jpg"}
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = {
            "btn-salvar": "1",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "1.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Teste",
            "ENDERECO": "Rua de Teste, 123",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            # form_token ausente
        }
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("foto.jpg", _JPEG_MINIMO, content_type="image/jpeg")}
        self.client.post("/cadastro-obras/", {**dados, **arquivos})
        self.assertEqual(self.colecao_obras.count_documents({"NOME_EMPRESA": "Empresa Teste"}), 0)

    @patch("obras.views.cloudinary.uploader.upload")
    def test_mesmo_token_em_cadastro_obras_cria_apenas_uma_vez(self, mock_upload):
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto.jpg"}
        from django.core.files.uploadedfile import SimpleUploadedFile
        token = _gerar_form_token()
        dados = {
            "btn-salvar": "1",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "1.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Token Teste",
            "ENDERECO": "Rua de Teste, 123",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": token,
        }
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("foto.jpg", _JPEG_MINIMO, content_type="image/jpeg")}
        self.client.post("/cadastro-obras/", {**dados, **arquivos})
        # Segunda submissão com o mesmo token
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto2.jpg"}
        self.client.post("/cadastro-obras/", {**dados, **arquivos})
        # Só deve ter criado uma obra
        self.assertEqual(self.colecao_obras.count_documents({"NOME_EMPRESA": "Empresa Token Teste"}), 1)

    def test_token_ausente_em_salva_edicao_obra_e_rejeitado(self):
        dados = {
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "2.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Editada",
            "ENDERECO": "Rua Editada, 456",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            # form_token ausente
        }
        self.client.post("/salvar-edicao-obra/", dados)
        doc = self.colecao_obras.find_one({"ID_OBRA": "12026"})
        self.assertEqual(doc["NOME_EMPRESA"], "Empresa Original")

    def test_mesmo_token_em_salva_edicao_obra_salva_apenas_uma_vez(self):
        token = _gerar_form_token()
        dados = {
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "2.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Editada",
            "ENDERECO": "Rua Editada, 456",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": token,
        }
        self.client.post("/salvar-edicao-obra/", dados)
        # Segunda submissão com token diferente mas campo alterado — só a primeira deve ter salvo
        dados2 = dict(dados)
        dados2["form_token"] = token  # mesmo token
        dados2["NOME_EMPRESA"] = "Empresa Segunda Tentativa"
        self.client.post("/salvar-edicao-obra/", dados2)
        doc = self.colecao_obras.find_one({"ID_OBRA": "12026"})
        # O segundo POST foi bloqueado pelo token reutilizado — nome continua o da primeira edição
        self.assertEqual(doc["NOME_EMPRESA"], "Empresa Editada")


# ==============================================================================
# EDIÇÃO DE OBRA COM NOVAS FOTOS
# ==============================================================================

class EdicaoObraComFotoTestCase(MongoTesteBase):
    """Cobre salva_edicao_obra quando novas fotos são enviadas."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )
        self.client.force_login(self.user)
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": 1000.0,
            "DATA_INICIO": "01/01/2026",
            "CONCLUSAO_PREVISTA": "31/12/2026",
            "DATA_FINALIZACAO": "—",
            "NOME_EMPRESA": "Empresa Original",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "EMPRESA_CONTRATADA": "EMPRESA ORIGINAL - CNPJ - " + CNPJ_VALIDO,
            "ENDERECO": "Rua Original, 1",
            "GALERIA": [],
        })

    def _dados_edicao(self, **overrides):
        dados = {
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "2.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Editada",
            "ENDERECO": "Rua Editada, 456",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": _gerar_form_token(),
        }
        dados.update(overrides)
        return dados

    @patch("obras.views.cloudinary.uploader.upload")
    def test_edicao_com_foto_jpeg_valida_salva_e_faz_upload(self, mock_upload):
        from django.core.files.uploadedfile import SimpleUploadedFile
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/nova.jpg"}
        dados = self._dados_edicao()
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("nova.jpg", _JPEG_MINIMO, content_type="image/jpeg")}
        self.client.post("/salvar-edicao-obra/", {**dados, **arquivos})
        mock_upload.assert_called_once()
        doc = self.colecao_obras.find_one({"ID_OBRA": "12026"})
        self.assertEqual(doc["NOME_EMPRESA"], "Empresa Editada")

    @patch("obras.views.cloudinary.uploader.upload")
    def test_edicao_com_magic_bytes_invalidos_e_rejeitada(self, mock_upload):
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = self._dados_edicao()
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("falso.jpg", _CONTEUDO_INVALIDO, content_type="image/jpeg")}
        self.client.post("/salvar-edicao-obra/", {**dados, **arquivos})
        mock_upload.assert_not_called()
        doc = self.colecao_obras.find_one({"ID_OBRA": "12026"})
        # Obra não deve ter sido atualizada
        self.assertEqual(doc["NOME_EMPRESA"], "Empresa Original")

    @patch("obras.views.cloudinary.uploader.upload")
    def test_edicao_com_mais_de_10_fotos_e_rejeitada(self, mock_upload):
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = self._dados_edicao()
        fotos = [
            SimpleUploadedFile(f"foto{i}.jpg", _JPEG_MINIMO, content_type="image/jpeg")
            for i in range(11)
        ]
        self.client.post("/salvar-edicao-obra/", {**dados, "FOTO_OBRA": fotos})
        mock_upload.assert_not_called()
        doc = self.colecao_obras.find_one({"ID_OBRA": "12026"})
        self.assertEqual(doc["NOME_EMPRESA"], "Empresa Original")


# ==============================================================================
# CONTROLE DE ACESSO — FUNCIONÁRIO COMUM EM ENDPOINTS DE SUPERVISOR
# ==============================================================================

class ControleAcessoComumTestCase(MongoTesteBase):
    """Funcionário COMUM autenticado não pode acessar endpoints de supervisor."""

    def setUp(self):
        super().setUp()
        self.comum = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Comum Teste"
        )
        self.client.force_login(self.comum)
        # Funcionário alvo para tentativa de edição
        User.objects.create_user(username="00000000191", password=SENHA_FORTE, first_name="Alvo")
        self.colecao_funcionarios.insert_one({
            "CPF": "00000000191",
            "NOME": "ALVO TESTE",
            "DATA_NASCIMENTO": "20/05/1990",
            "RG": "123456789",
            "FUNCAO": "COMUM",
        })
        session = self.client.session
        session["cpf_editando"] = "00000000191"
        session.save()

    def test_comum_nao_pode_acessar_salva_edicao_funcionario(self):
        dados = {
            "CPF_ORIGINAL": "00000000191",
            "NOME": "Tentativa de Alteracao",
            "DATA_NASCIMENTO": "1990-05-20",
            "RG": "123456789",
            "FUNCAO": "COMUM",
            "SENHA": "",
            "form_token": _gerar_form_token(),
        }
        resposta = self.client.post("/salvar-edicao-funcionario/", dados)
        self.assertRedirects(resposta, "/inicio/")
        doc = self.colecao_funcionarios.find_one({"CPF": "00000000191"})
        self.assertEqual(doc["NOME"], "ALVO TESTE")

    def test_comum_nao_pode_acessar_busca_atualiza_funcionario(self):
        resposta = self.client.get("/atualizar-funcionario/")
        self.assertRedirects(resposta, "/inicio/")


# ==============================================================================
# BUSCA DE OBRA — SESSÃO
# ==============================================================================

class BuscaObraSessionTestCase(MongoTesteBase):
    """Verifica comportamentos de sessão na busca de obra."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao"
        )
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "VALOR_OBRA": 1000.0,
            "DATA_INICIO": "01/01/2026",
            "CONCLUSAO_PREVISTA": "31/12/2026",
            "DATA_FINALIZACAO": "—",
            "NOME_EMPRESA": "Empresa Teste",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "TIPO_EXECUCAO": "Nova Construção",
            "ENDERECO": "Rua de Teste, 123",
        })
        self.client.force_login(self.user)

    def test_busca_obra_id_vazio_exibe_erro(self):
        resposta = self.client.post(
            "/atualizar-obra/",
            {"ID_OBRA": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 200)


# ==============================================================================
# _EXTRAIR_PUBLIC_ID_CLOUDINARY — UNIT TESTS (sem Mongo)
# ==============================================================================

class ExtrairPublicIdCloudinaryTestCase(TestCase):
    """Testa a função pura de extração de public_id de URLs do Cloudinary."""

    def test_url_com_versao_retorna_public_id_sem_versao(self):
        url = "https://res.cloudinary.com/demo/image/upload/v1234567890/obras/foto.jpg"
        self.assertEqual(_extrair_public_id_cloudinary(url), "obras/foto")

    def test_url_sem_versao_retorna_public_id(self):
        url = "https://res.cloudinary.com/demo/image/upload/obras/foto.jpg"
        self.assertEqual(_extrair_public_id_cloudinary(url), "obras/foto")

    def test_url_sem_upload_retorna_none(self):
        url = "https://res.cloudinary.com/demo/image/foto.jpg"
        self.assertIsNone(_extrair_public_id_cloudinary(url))

    def test_url_invalida_retorna_none(self):
        self.assertIsNone(_extrair_public_id_cloudinary("nao-e-uma-url"))

    def test_url_vazia_retorna_none(self):
        self.assertIsNone(_extrair_public_id_cloudinary(""))

    def test_url_com_extensao_webp_retorna_sem_extensao(self):
        url = "https://res.cloudinary.com/demo/image/upload/v111/pasta/img.webp"
        self.assertEqual(_extrair_public_id_cloudinary(url), "pasta/img")

    def test_url_com_multiplos_subdiretorios_preserva_caminho(self):
        url = "https://res.cloudinary.com/demo/image/upload/v1/a/b/c/foto.png"
        self.assertEqual(_extrair_public_id_cloudinary(url), "a/b/c/foto")


# ==============================================================================
# ZONA ADMINISTRATIVA — ACESSO E LISTAGEM
# ==============================================================================

class ZonaAdminAcessoTestCase(MongoTesteBase):
    """Testa controle de acesso e renderização da zona_admin."""

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor"
        )
        self.comum = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Comum"
        )
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "ENDERECO": "Rua de Teste, 123",
            "DATA_CADASTRO": "01/01/2026 10:00:00",
            "GALERIA": [],
            "TIMESTAMP_CADASTRO": __import__("datetime").datetime.now(),
        })

    def test_nao_autenticado_redireciona_para_login_com_next(self):
        resposta = self.client.get("/zona-admin/")
        self.assertTrue(resposta.url.startswith("/login/"))
        self.assertIn("zona-admin", resposta.url)

    def test_funcionario_comum_acessa_aba_obras(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/zona-admin/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_funcionario_comum_nao_acessa_aba_funcionarios(self):
        self.client.force_login(self.comum)
        resposta = self.client.get("/zona-admin/?aba=funcionarios", HTTP_HX_REQUEST="true")
        self.assertRedirects(resposta, "/zona-admin/", fetch_redirect_response=False)

    def test_supervisor_acessa_htmx_retorna_200(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/zona-admin/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_supervisor_acessa_direto_retorna_200(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/zona-admin/")
        self.assertEqual(resposta.status_code, 200)

    def test_obras_aparecem_na_listagem(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/zona-admin/", HTTP_HX_REQUEST="true")
        self.assertContains(resposta, "12026")

    def test_zona_admin_exige_autenticacao(self):
        """Confirma que a rota exige login — client sem login deve ser redirecionado."""
        resposta = self.client.get("/zona-admin/")
        self.assertIn(resposta.status_code, (301, 302))
        self.assertIn("/login/", resposta.url)

    def test_comum_nao_acessa_aba_funcionarios(self):
        """Funcionário COMUM que tenta acessar aba=funcionarios é redirecionado de volta para zona_admin."""
        self.client.force_login(self.comum)
        resposta = self.client.get("/zona-admin/?aba=funcionarios")
        self.assertRedirects(resposta, "/zona-admin/", fetch_redirect_response=False)


# ==============================================================================
# ZONA DE EXCLUSÃO — DELEÇÃO DE OBRAS
# ==============================================================================

class DeletarObraTestCase(MongoTesteBase):
    """Testa a view deletar_obra: acesso, exclusão do Mongo, timelapse e respostas."""

    ID_OBRA_TESTE = "12026"

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username="52998224725", password=SENHA_FORTE, is_staff=True, first_name="Supervisor"
        )
        self.comum = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Comum"
        )
        self.colecao_obras.insert_one({
            "ID_OBRA": self.ID_OBRA_TESTE,
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "ENDERECO": "Rua A",
            "GALERIA": ["https://res.cloudinary.com/demo/image/upload/v1/obras/foto.jpg"],
            "TIMESTAMP_CADASTRO": __import__("datetime").datetime.now(),
        })
        self.colecao_timelapse.insert_one({
            "ID_OBRA": self.ID_OBRA_TESTE,
            "URL": "https://res.cloudinary.com/demo/image/upload/v1/tl/foto.jpg",
        })

    # ------------------------------------------------------------------
    # Controle de acesso
    # ------------------------------------------------------------------

    def test_nao_autenticado_redireciona_para_login(self):
        resposta = self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE})
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_funcionario_comum_e_redirecionado_para_inicio(self):
        self.client.force_login(self.comum)
        resposta = self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE})
        self.assertRedirects(resposta, "/inicio/")
        # Obra não deve ter sido deletada
        self.assertEqual(self.colecao_obras.count_documents({"ID_OBRA": self.ID_OBRA_TESTE}), 1)

    def test_get_nao_permitido_retorna_405(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.get("/deletar-obra/")
        self.assertEqual(resposta.status_code, 405)

    # ------------------------------------------------------------------
    # Fluxo feliz
    # ------------------------------------------------------------------

    @patch("obras.views.cloudinary.uploader.destroy")
    def test_supervisor_deleta_obra_existente(self, mock_destroy):
        self.client.force_login(self.supervisor)
        self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()})
        self.assertEqual(self.colecao_obras.count_documents({"ID_OBRA": self.ID_OBRA_TESTE}), 0)

    @patch("obras.views.cloudinary.uploader.destroy")
    def test_timelapse_e_deletado_junto_com_a_obra(self, mock_destroy):
        self.client.force_login(self.supervisor)
        self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()})
        self.assertEqual(self.colecao_timelapse.count_documents({"ID_OBRA": self.ID_OBRA_TESTE}), 0)

    @patch("obras.views.cloudinary.uploader.destroy")
    def test_cloudinary_destroy_e_chamado_para_cada_foto(self, mock_destroy):
        self.client.force_login(self.supervisor)
        self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()})
        mock_destroy.assert_called_once()
        # Verifica que o public_id extraído foi passado corretamente
        args, _ = mock_destroy.call_args
        self.assertIn("obras/foto", args[0])

    @patch("obras.views.cloudinary.uploader.destroy")
    def test_resposta_htmx_tem_hx_redirect(self, mock_destroy):
        self.client.force_login(self.supervisor)
        resposta = self.client.post(
            "/deletar-obra/",
            {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 204)
        self.assertIn("HX-Redirect", resposta)

    @patch("obras.views.cloudinary.uploader.destroy")
    def test_resposta_nao_htmx_redireciona_normalmente(self, mock_destroy):
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()})
        self.assertRedirects(resposta, "/zona-admin/", fetch_redirect_response=False)

    # ------------------------------------------------------------------
    # Casos de erro
    # ------------------------------------------------------------------

    def test_id_vazio_nao_deleta_nada(self):
        self.client.force_login(self.supervisor)
        self.client.post("/deletar-obra/", {"id_obra": "", "form_token": _gerar_form_token()})
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    def test_id_obra_inexistente_nao_quebra(self):
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/deletar-obra/", {"id_obra": "99999", "form_token": _gerar_form_token()})
        # Deve redirecionar com mensagem de erro, sem 500
        self.assertEqual(resposta.status_code, 302)

    @patch("obras.views.cloudinary.uploader.destroy", side_effect=Exception("Cloudinary fora"))
    def test_falha_no_cloudinary_nao_impede_exclusao_do_mongo(self, mock_destroy):
        """Cloudinary é melhor esforço — falha não deve impedir a deleção da obra."""
        self.client.force_login(self.supervisor)
        self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()})
        # Obra ainda deve ter sido deletada do Mongo mesmo com Cloudinary falhando
        self.assertEqual(self.colecao_obras.count_documents({"ID_OBRA": self.ID_OBRA_TESTE}), 0)

    # ------------------------------------------------------------------
    # Acesso sem autenticação à zona de exclusão (complementa AcessoNaoAutenticadoTestCase)
    # ------------------------------------------------------------------

    def test_zona_admin_get_redireciona_para_login(self):
        resposta = self.client.get("/zona-admin/")
        self.assertTrue(resposta.url.startswith("/login/"))

    def test_token_ausente_e_rejeitado(self):
        """POST sem form_token deve ser rejeitado pelo guard de idempotência."""
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE})
        self.assertEqual(self.colecao_obras.count_documents({"ID_OBRA": self.ID_OBRA_TESTE}), 1)
        self.assertRedirects(resposta, "/zona-admin/", fetch_redirect_response=False)

    @patch("obras.views.cloudinary.uploader.destroy")
    def test_audit_log_registra_exclusao(self, mock_destroy):
        """Deve emitir logger.warning com ID da obra e usuário ao deletar com sucesso."""
        self.client.force_login(self.supervisor)
        with self.assertLogs("obras.views", level="WARNING") as cm:
            self.client.post("/deletar-obra/", {"id_obra": self.ID_OBRA_TESTE, "form_token": _gerar_form_token()})
        self.assertTrue(any(self.ID_OBRA_TESTE in line for line in cm.output))


# ==============================================================================
# DASHBOARD PÚBLICO
# ==============================================================================

class DashboardPublicoTestCase(MongoTesteBase):
    """dashboard_publico é público (sem autenticação) e suporta dual-render."""

    def test_acessivel_sem_autenticacao(self):
        resposta = self.client.get("/dashboard-obras/")
        self.assertEqual(resposta.status_code, 200)

    def test_retorna_partial_em_request_htmx(self):
        resposta = self.client.get("/dashboard-obras/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "dashboard_obras.html")
        self.assertTemplateNotUsed(resposta, "index.html")

    def test_retorna_shell_em_acesso_direto(self):
        resposta = self.client.get("/dashboard-obras/")
        self.assertTemplateUsed(resposta, "index.html")
        self.assertTemplateUsed(resposta, "dashboard_obras.html")

    def test_cards_de_totais_presentes_no_contexto(self):
        self.colecao_obras.insert_one({
            "ID_OBRA": "12026",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": 100000.0,
            "DATA_INICIO": "01/01/2026",
            "EMPRESA_CONTRATADA": "Empresa A",
            "TIMESTAMP_CADASTRO": __import__("datetime").datetime.now(),
        })
        resposta = self.client.get("/dashboard-obras/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.context["total"], 1)
        self.assertEqual(resposta.context["em_execucao"], 1)

    def test_funciona_com_banco_vazio(self):
        """Com coleção vazia, não deve lançar exceção — retorna zeros."""
        resposta = self.client.get("/dashboard-obras/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.context["total"], 0)


# ==============================================================================
# SESSÃO EXPIRADA — MIDDLEWARE
# ==============================================================================

class SessaoExpiradaMiddlewareTestCase(MongoTesteBase):
    """SessaoExpiradaMiddleware detecta cookie stale e redireciona para login."""

    def test_cookie_stale_redireciona_para_login(self):
        """Simula um cookie de sessão que não existe mais no banco."""
        self.client.cookies["sessionid"] = "sessao_que_nao_existe_no_banco_xpto123"
        resposta = self.client.get("/inicio/")
        self.assertRedirects(resposta, "/login/", fetch_redirect_response=False)

    def test_cookie_stale_seta_aviso_inatividade_na_sessao(self):
        """Cookie stale deve setar aviso_login='inatividade' para exibir mensagem correta no login."""
        self.client.cookies["sessionid"] = "cookie_stale_para_teste_aviso_xyz"
        self.client.get("/inicio/")
        # Após o redirect, a nova sessão deve ter o aviso
        self.assertEqual(self.client.session.get('aviso_login'), 'inatividade')

    def test_pagina_publica_com_cookie_stale_nao_redireciona(self):
        """Visitante com cookie stale em página pública não deve ser redirecionado para login."""
        self.client.cookies["sessionid"] = "cookie_stale_visitante_publico_abc"
        resposta = self.client.get("/lista-obras/")
        self.assertEqual(resposta.status_code, 200)
        resposta2 = self.client.get("/dashboard-obras/")
        self.assertEqual(resposta2.status_code, 200)

    def test_sessao_valida_nao_e_redirecionada(self):
        user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao"
        )
        self.client.force_login(user)
        resposta = self.client.get("/inicio/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 200)

    def test_rota_login_nao_dispara_middleware(self):
        """Acesso direto ao /login/ com cookie stale não deve causar redirect loop."""
        self.client.cookies["sessionid"] = "cookie_invalido_xyz"
        resposta = self.client.get("/login/")
        # Deve renderizar normalmente, não entrar em loop de redirect
        self.assertEqual(resposta.status_code, 200)

    def test_rota_logout_nao_dispara_middleware(self):
        """POST em /logout/ com cookie stale não deve causar redirect indevido."""
        self.client.cookies["sessionid"] = "cookie_invalido_xyz"
        resposta = self.client.post("/logout/")
        # Deve redirecionar para login normalmente (logout_view), não ser interceptado
        self.assertEqual(resposta.status_code, 302)


# ==============================================================================
# UPLOAD WEBP
# ==============================================================================

class UploadWebpTestCase(MongoTesteBase):
    """Valida que arquivos WEBP são aceitos pela validação de magic bytes."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, first_name="Joao Teste"
        )
        self.client.force_login(self.user)

    def _dados_obra(self):
        return {
            "btn-salvar": "1",
            "TIPO_OBRA": "PAVIMENTAÇÃO",
            "SITUACAO": "Em andamento",
            "TIPO_EXECUCAO": "Nova Construção",
            "VALOR_OBRA": "1.000,00",
            "DATA_INICIO": "2026-01-01",
            "CONCLUSAO_PREVISTA": "2026-12-31",
            "DATA_FINALIZACAO": "",
            "NOME_EMPRESA": "Empresa Teste",
            "ENDERECO": "Rua de Teste, 123",
            "CNPJ_EMPRESA": CNPJ_VALIDO,
            "form_token": _gerar_form_token(),
        }

    @patch("obras.views.cloudinary.uploader.upload")
    def test_webp_valido_e_aceito(self, mock_upload):
        from django.core.files.uploadedfile import SimpleUploadedFile
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/teste/foto.webp"}
        dados = self._dados_obra()
        arquivo = SimpleUploadedFile("foto.webp", _WEBP_MINIMO, content_type="image/webp")
        self.client.post("/cadastro-obras/", {**dados, "FOTO_OBRA": arquivo})
        self.assertEqual(self.colecao_obras.count_documents({}), 1)

    def test_webp_com_magic_bytes_invalidos_rejeitado(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = self._dados_obra()
        conteudo_invalido = b'RIFF\x00\x00\x00\x00WEBX' + b'\x00' * 4  # WEBX em vez de WEBP
        arquivo = SimpleUploadedFile("falso.webp", conteudo_invalido, content_type="image/webp")
        self.client.post("/cadastro-obras/", {**dados, "FOTO_OBRA": arquivo})
        self.assertEqual(self.colecao_obras.count_documents({}), 0)


# ==============================================================================
# SESSÃO ÚNICA — PATH HTMX (HX-Redirect)
# ==============================================================================

class SessaoUnicaHtmxTestCase(MongoTesteBase):
    """Cobre o caminho HTMX do SessaoUnicaMiddleware (resposta 204 + HX-Redirect)."""

    def setUp(self):
        super().setUp()
        self.cpf = CPF_VALIDO
        User.objects.create_user(username=self.cpf, password=SENHA_FORTE, first_name="Joao Teste")

    def test_segundo_login_derruba_sessao_htmx_com_hx_redirect(self):
        cliente_1 = Client()
        cliente_2 = Client()

        cliente_1.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        cliente_2.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})

        # Requisição HTMX com sessão stale deve receber 204 + HX-Redirect
        resposta = cliente_1.get("/inicio/", HTTP_HX_REQUEST="true")
        self.assertEqual(resposta.status_code, 204)
        self.assertIn("HX-Redirect", resposta)
        self.assertIn("/login/", resposta["HX-Redirect"])

    def test_segundo_login_derruba_sessao_normal_com_redirect(self):
        cliente_1 = Client()
        cliente_2 = Client()

        cliente_1.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        cliente_2.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})

        # Requisição normal com sessão stale deve receber redirect 302
        resposta = cliente_1.get("/inicio/")
        self.assertRedirects(resposta, "/login/", fetch_redirect_response=False)


# ==============================================================================
# DELETAR FUNCIONÁRIO
# ==============================================================================

class DeletarFuncionarioTestCase(MongoTesteBase):
    """Testa a view deletar_funcionario: acesso, deleção e respostas.

    Nota: deletar_funcionario exige is_superuser (Gerente Geral), não apenas is_staff.
    """

    CPF_ALVO = "44455566677"
    CPF_GERENTE = "52998224725"

    def setUp(self):
        super().setUp()
        # Gerente Geral: único que pode deletar funcionários
        self.gerente = User.objects.create_superuser(
            username=self.CPF_GERENTE, password=SENHA_FORTE, first_name="Gerente"
        )
        # Supervisor (is_staff mas não is_superuser) — também não pode deletar
        self.supervisor = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, is_staff=True, first_name="Supervisor"
        )
        # Funcionário alvo da deleção
        self.alvo = User.objects.create_user(
            username=self.CPF_ALVO, password=SENHA_FORTE, first_name="Funcionario Alvo"
        )
        self.colecao_funcionarios.insert_one({
            "CPF": self.CPF_ALVO,
            "NOME": "Funcionario Alvo",
            "FUNCAO": "COMUM",
        })

    def _payload(self):
        return {"cpf": self.CPF_ALVO, "form_token": _gerar_form_token()}

    # --- Controle de acesso ---

    def test_nao_autenticado_redireciona_para_login(self):
        resposta = self.client.post("/deletar-funcionario/", self._payload())
        self.assertIn("/login/", resposta.url)

    def test_supervisor_nao_pode_deletar(self):
        """is_staff sem is_superuser é bloqueado — view exige Gerente Geral."""
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/deletar-funcionario/", self._payload())
        # View redireciona para zona_admin com mensagem de erro
        self.assertIn(resposta.status_code, (301, 302))
        # Funcionário alvo não deve ter sido deletado
        self.assertEqual(self.colecao_funcionarios.count_documents({"CPF": self.CPF_ALVO}), 1)
        self.assertTrue(User.objects.filter(username=self.CPF_ALVO).exists())

    def test_get_nao_permitido_retorna_405(self):
        self.client.force_login(self.gerente)
        resposta = self.client.get("/deletar-funcionario/")
        self.assertEqual(resposta.status_code, 405)

    def test_token_ausente_e_rejeitado(self):
        self.client.force_login(self.gerente)
        resposta = self.client.post("/deletar-funcionario/", {"cpf": self.CPF_ALVO})
        self.assertEqual(self.colecao_funcionarios.count_documents({"CPF": self.CPF_ALVO}), 1)
        self.assertTrue(User.objects.filter(username=self.CPF_ALVO).exists())

    # --- Fluxo feliz ---

    def test_gerente_deleta_funcionario(self):
        self.client.force_login(self.gerente)
        self.client.post("/deletar-funcionario/", self._payload())
        self.assertEqual(self.colecao_funcionarios.count_documents({"CPF": self.CPF_ALVO}), 0)
        self.assertFalse(User.objects.filter(username=self.CPF_ALVO).exists())

    def test_resposta_htmx_tem_hx_redirect(self):
        self.client.force_login(self.gerente)
        resposta = self.client.post(
            "/deletar-funcionario/",
            self._payload(),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 204)
        self.assertIn("HX-Redirect", resposta)

    def test_cpf_inexistente_nao_quebra(self):
        self.client.force_login(self.gerente)
        resposta = self.client.post(
            "/deletar-funcionario/",
            {"cpf": "99999999999", "form_token": _gerar_form_token()},
        )
        self.assertIn(resposta.status_code, (301, 302))

    def test_nao_pode_deletar_outro_superusuario(self):
        outro_gerente = User.objects.create_superuser(
            username="00000000000", password=SENHA_FORTE, first_name="Outro Gerente"
        )
        self.colecao_funcionarios.insert_one({"CPF": "00000000000", "NOME": "Outro Gerente"})
        self.client.force_login(self.gerente)
        resposta = self.client.post(
            "/deletar-funcionario/",
            {"cpf": "00000000000", "form_token": _gerar_form_token()},
        )
        self.assertRedirects(resposta, "/zona-admin/?aba=funcionarios", fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username="00000000000").exists())


# ==============================================================================
# ALTERAR CARGO DE FUNCIONÁRIO
# ==============================================================================

class AlterarCargoFuncionarioTestCase(MongoTesteBase):
    """Testa a view alterar_cargo_funcionario: acesso, promoção, rebaixamento e guards."""

    CPF_ALVO = "44455566677"
    CPF_GERENTE = "52998224725"

    def setUp(self):
        super().setUp()
        self.gerente = User.objects.create_superuser(
            username=self.CPF_GERENTE, password=SENHA_FORTE, first_name="Gerente"
        )
        self.supervisor = User.objects.create_user(
            username=CPF_VALIDO, password=SENHA_FORTE, is_staff=True, first_name="Supervisor"
        )
        self.alvo = User.objects.create_user(
            username=self.CPF_ALVO, password=SENHA_FORTE, is_staff=False, first_name="Funcionario Alvo"
        )
        self.colecao_funcionarios.insert_one({
            "CPF": self.CPF_ALVO,
            "NOME": "Funcionario Alvo",
            "FUNCAO": "COMUM",
        })

    def _payload(self, novo_cargo="SUPERVISOR"):
        return {"cpf": self.CPF_ALVO, "novo_cargo": novo_cargo, "form_token": _gerar_form_token()}

    # --- Controle de acesso ---

    def test_nao_autenticado_redireciona_para_login(self):
        resposta = self.client.post("/alterar-cargo/", self._payload())
        self.assertIn("/login/", resposta.url)

    def test_supervisor_nao_pode_alterar_cargo(self):
        """is_staff sem is_superuser não pode alterar cargos."""
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/alterar-cargo/", self._payload())
        self.assertIn(resposta.status_code, (301, 302))
        self.alvo.refresh_from_db()
        self.assertFalse(self.alvo.is_staff)

    def test_get_nao_permitido_retorna_405(self):
        self.client.force_login(self.gerente)
        resposta = self.client.get("/alterar-cargo/")
        self.assertEqual(resposta.status_code, 405)

    def test_token_ausente_e_rejeitado(self):
        self.client.force_login(self.gerente)
        self.client.post("/alterar-cargo/", {"cpf": self.CPF_ALVO, "novo_cargo": "SUPERVISOR"})
        self.alvo.refresh_from_db()
        self.assertFalse(self.alvo.is_staff)

    def test_cargo_invalido_e_rejeitado(self):
        self.client.force_login(self.gerente)
        resposta = self.client.post("/alterar-cargo/", {
            "cpf": self.CPF_ALVO, "novo_cargo": "DEUS", "form_token": _gerar_form_token()
        })
        self.assertIn(resposta.status_code, (301, 302))
        self.alvo.refresh_from_db()
        self.assertFalse(self.alvo.is_staff)

    def test_nao_pode_alterar_proprio_cargo(self):
        self.client.force_login(self.gerente)
        resposta = self.client.post("/alterar-cargo/", {
            "cpf": self.CPF_GERENTE, "novo_cargo": "COMUM", "form_token": _gerar_form_token()
        })
        self.assertIn(resposta.status_code, (301, 302))
        self.gerente.refresh_from_db()
        self.assertTrue(self.gerente.is_superuser)

    def test_nao_pode_alterar_cargo_de_outro_gerente(self):
        outro_gerente = User.objects.create_superuser(
            username="00000000000", password=SENHA_FORTE, first_name="Outro Gerente"
        )
        self.client.force_login(self.gerente)
        resposta = self.client.post("/alterar-cargo/", {
            "cpf": "00000000000", "novo_cargo": "COMUM", "form_token": _gerar_form_token()
        })
        self.assertIn(resposta.status_code, (301, 302))
        outro_gerente.refresh_from_db()
        self.assertTrue(outro_gerente.is_superuser)

    # --- Fluxo feliz ---

    def test_gerente_promove_comum_para_supervisor(self):
        self.client.force_login(self.gerente)
        self.client.post("/alterar-cargo/", self._payload("SUPERVISOR"))
        self.alvo.refresh_from_db()
        self.assertTrue(self.alvo.is_staff)
        self.assertFalse(self.alvo.is_superuser)

    def test_gerente_rebaixa_supervisor_para_comum(self):
        self.alvo.is_staff = True
        self.alvo.save()
        self.client.force_login(self.gerente)
        self.client.post("/alterar-cargo/", self._payload("COMUM"))
        self.alvo.refresh_from_db()
        self.assertFalse(self.alvo.is_staff)

    def test_resposta_htmx_tem_hx_redirect(self):
        self.client.force_login(self.gerente)
        resposta = self.client.post(
            "/alterar-cargo/",
            self._payload(),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resposta.status_code, 204)
        self.assertIn("HX-Redirect", resposta)


# ==============================================================================
# UTILS — formatar_data_br e preparar_data_para_input
# ==============================================================================

class UtilsDataTestCase(TestCase):
    """Funções puras de conversão de data — sem Mongo, sem rede."""

    def test_formatar_data_br_formato_correto(self):
        self.assertEqual(formatar_data_br("2026-07-12"), "12/07/2026")

    def test_formatar_data_br_vazio_retorna_travessao(self):
        self.assertEqual(formatar_data_br(""), "—")

    def test_formatar_data_br_travessao_passado_retorna_travessao(self):
        self.assertEqual(formatar_data_br("—"), "—")

    def test_formatar_data_br_formato_invalido_retorna_travessao(self):
        # VP-5: antes retornava o input inválido; agora deve retornar '—'
        self.assertEqual(formatar_data_br("2026-01-15T00:00:00"), "—")
        self.assertEqual(formatar_data_br("15/01/2026"), "—")
        self.assertEqual(formatar_data_br("nao-e-uma-data"), "—")

    def test_preparar_data_para_input_formato_correto(self):
        self.assertEqual(preparar_data_para_input("12/07/2026"), "2026-07-12")

    def test_preparar_data_para_input_travessao_retorna_vazio(self):
        self.assertEqual(preparar_data_para_input("—"), "")

    def test_preparar_data_para_input_formato_invalido_retorna_vazio(self):
        self.assertEqual(preparar_data_para_input("2026-07-12"), "")


# ==============================================================================
# SESSÃO EXPIRADA — HX-Redirect em requests HTMX (VP-3)
# ==============================================================================

class SessaoExpiradaHtmxTestCase(TestCase):
    """Garante que SessaoExpiradaMiddleware emite HX-Redirect (não 302) em HTMX."""

    def _criar_sessao_stale(self):
        """Cria um cookie de sessão válido no banco, depois destroi os dados
        da sessão no banco para simular expiração — cookie existe mas sessão está vazia."""
        from django.contrib.sessions.backends.db import SessionStore
        s = SessionStore()
        s['_placeholder'] = True
        s.save()
        session_key = s.session_key
        # Simula expiração: deleta os dados deixando o key inacessível
        s.delete()
        return session_key

    def test_sessao_expirada_em_request_htmx_retorna_hx_redirect(self):
        session_key = self._criar_sessao_stale()
        from django.conf import settings as dj_settings
        # Injeta o cookie stale no client sem usar force_login
        self.client.cookies[dj_settings.SESSION_COOKIE_NAME] = session_key
        resposta = self.client.get("/inicio/", HTTP_HX_REQUEST="true")
        # Deve responder 204 com HX-Redirect, não 302
        self.assertEqual(resposta.status_code, 204)
        self.assertIn("HX-Redirect", resposta)
        self.assertIn("/login/", resposta["HX-Redirect"])

    def test_sessao_expirada_em_request_normal_retorna_302(self):
        session_key = self._criar_sessao_stale()
        from django.conf import settings as dj_settings
        self.client.cookies[dj_settings.SESSION_COOKIE_NAME] = session_key
        resposta = self.client.get("/inicio/")
        # Sem HX-Request: redirect normal
        self.assertIn(resposta.status_code, (301, 302))
        self.assertNotIn("HX-Redirect", resposta)


# ==============================================================================
# SALVA EDIÇÃO FUNCIONÁRIO — falha MongoDB não gera 500 (VP-1)
# ==============================================================================

class SalvaEdicaoFuncionarioFalhaMongoTestCase(MongoTesteBase):
    """Garante que erro no MongoDB em salva_edicao_funcionario devolve redirect
    amigável ao usuário em vez de página de erro 500."""

    CPF_SUPERVISOR = "52998224725"
    CPF_ALVO = "44455566677"

    def setUp(self):
        super().setUp()
        self.supervisor = User.objects.create_user(
            username=self.CPF_SUPERVISOR, password=SENHA_FORTE,
            is_staff=True, first_name="Supervisor Teste"
        )
        self.alvo = User.objects.create_user(
            username=self.CPF_ALVO, password=SENHA_FORTE,
            is_staff=False, first_name="Funcionario Alvo"
        )
        self.colecao_funcionarios.insert_one({
            "CPF": self.CPF_ALVO,
            "NOME": "FUNCIONARIO ALVO",
            "FUNCAO": "COMUM",
            "RG": "123456789",
            "DATA_NASCIMENTO": "01/01/1990",
        })

    def _payload_edicao(self):
        token = _gerar_form_token()
        self.client.session['cpf_editando'] = self.CPF_ALVO
        session = self.client.session
        session['cpf_editando'] = self.CPF_ALVO
        session.save()
        return {
            "form_token": token,
            "NOME": "Funcionario Alvo Editado",
            "DATA_NASCIMENTO": "1990-01-01",
            "RG": "123456789",
            "FUNCAO": "COMUM",
            "SENHA": "",
        }

    @patch("obras.views.colecao_funcionarios")
    def test_falha_mongodb_retorna_redirect_nao_500(self, mock_col):
        """update_one lança exceção → deve redirecionar, não 500."""
        mock_col.find_one.return_value = {
            "CPF": self.CPF_ALVO,
            "NOME": "FUNCIONARIO ALVO",
            "FUNCAO": "COMUM",
            "RG": "123456789",
            "DATA_NASCIMENTO": "01/01/1990",
        }
        mock_col.update_one.side_effect = Exception("MongoDB timeout simulado")
        self.client.force_login(self.supervisor)
        resposta = self.client.post("/salvar-edicao-funcionario/", self._payload_edicao())
        # Deve redirecionar (PRG), não explodir com 500
        self.assertIn(resposta.status_code, (301, 302))
        self.assertNotEqual(resposta.status_code, 500)
