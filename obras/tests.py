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

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase
from pymongo import MongoClient

from obras import views as obras_views
from obras.views import (
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
        super().tearDownClass()

    def setUp(self):
        # Isola cada teste: começa sempre com as coleções de negócio vazias.
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
        self.assertRedirects(resposta, "/inicio/")

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
        self.assertRedirects(resposta, "/inicio/")


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
        from datetime import date, timedelta
        menor = (date.today() - timedelta(days=17 * 365)).strftime("%Y-%m-%d")
        self._postar(NIVEL_ACESSO="SUPERVISOR", DATA_NASCIMENTO=menor)
        self.assertFalse(self._funcionario_criado())

    def test_funcionario_comum_menor_de_16_anos_e_rejeitado(self):
        from datetime import date, timedelta
        menor = (date.today() - timedelta(days=15 * 365)).strftime("%Y-%m-%d")
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

    def test_senha_so_numeros_e_rejeitada(self):
        self._postar(SENHA="12345678")
        self.assertFalse(self._funcionario_criado())

    def test_senha_comum_e_rejeitada(self):
        self._postar(SENHA="password123")
        self.assertFalse(self._funcionario_criado())


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
        from datetime import date, timedelta
        menor = (date.today() - timedelta(days=17 * 365)).strftime("%Y-%m-%d")
        self._postar(FUNCAO="SUPERVISOR", DATA_NASCIMENTO=menor)
        self.assertFalse(self._funcionario_atualizado("Carlos da Silva Editado"))

    def test_comum_menor_de_16_anos_e_rejeitado(self):
        from datetime import date, timedelta
        menor = (date.today() - timedelta(days=15 * 365)).strftime("%Y-%m-%d")
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
            "VALOR_OBRA": "1.000,00",
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

    def test_login_correto_redireciona_para_inicio(self):
        resposta = self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertRedirects(resposta, "/inicio/")

    def test_login_correto_autentica_sessao(self):
        self.client.post("/login/", {"username": self.cpf, "password": SENHA_FORTE})
        self.assertTrue(self.client.session.get("_auth_user_id"))

    def test_login_com_cpf_formatado_pontos_tracos_funciona(self):
        """CPF com formatação visual deve ser limpo e autenticar normalmente."""
        cpf_formatado = "111.444.777-35"
        resposta = self.client.post("/login/", {"username": cpf_formatado, "password": SENHA_FORTE})
        self.assertRedirects(resposta, "/inicio/")

    def test_login_correto_redireciona_para_next_seguro(self):
        resposta = self.client.post(
            "/login/?next=/inicio/",
            {"username": self.cpf, "password": SENHA_FORTE},
        )
        self.assertRedirects(resposta, "/inicio/")

    def test_login_next_externo_redireciona_para_inicio(self):
        """Open redirect: ?next= apontando para domínio externo deve ser ignorado."""
        resposta = self.client.post(
            "/login/?next=http://malicioso.com",
            {"username": self.cpf, "password": SENHA_FORTE},
        )
        self.assertRedirects(resposta, "/inicio/")

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

    def test_usuario_ja_logado_pode_acessar_login_novamente(self):
        """Não há redirecionamento automático para usuário já autenticado — login
        é uma página pública que pode ser exibida mesmo logado."""
        self.client.force_login(self.user)
        resposta = self.client.get("/login/")
        self.assertEqual(resposta.status_code, 200)

    # ------------------------------------------------------------------
    # Conta superuser sem first_name (bug corrigido — IndexError)
    # ------------------------------------------------------------------

    def test_superuser_sem_first_name_faz_login_sem_erro(self):
        su = User.objects.create_superuser(username="00000000191", password=SENHA_FORTE)
        su.first_name = ""
        su.save()
        resposta = self.client.post("/login/", {"username": "00000000191", "password": SENHA_FORTE})
        self.assertRedirects(resposta, "/inicio/")


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

    def test_logout_por_inatividade_redireciona_com_aviso(self):
        resposta = self.client.post("/logout/", {"motivo": "inatividade"})
        self.assertIn("inatividade", resposta.url)

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
            "VALOR_OBRA": "1.000,00",
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
            "VALOR_OBRA": "1.000,00",
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
