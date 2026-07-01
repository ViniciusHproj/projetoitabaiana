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

    NOME_BANCO_TESTE = f"{os.environ['MONGODB_DB_NAME']}_teste"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
        arquivos = {"FOTO_OBRA": SimpleUploadedFile("foto.jpg", b"conteudo-fake", content_type="image/jpeg")}
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
