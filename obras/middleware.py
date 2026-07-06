import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse


class CSPNonceMiddleware:
    """
    Gera um nonce aleatório por request e o injeta em:
      - request.csp_nonce  (acessível nos templates via {{ request.csp_nonce }})
      - Content-Security-Policy header na resposta (enforcement completo)

    script-src: apenas scripts do próprio servidor ou com nonce válido por request.
    Nenhum inline sem nonce, nenhum script de CDN externo — proteção real contra XSS.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = uuid.uuid4().hex
        response = self.get_response(request)
        nonce = request.csp_nonce
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: res.cloudinary.com; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-src https://datastudio.google.com https://lookerstudio.google.com; "
            "object-src 'none'; "
            "form-action 'self'; "
            "base-uri 'self';"
        )
        response['Content-Security-Policy'] = csp
        return response


class SessaoExpiradaMiddleware:
    """
    Detecta quando o navegador enviou um cookie de sessão que o servidor não
    reconhece mais (sessão expirada por SESSION_COOKIE_AGE) e avisa o usuário
    que ele foi desconectado por inatividade — reforço server-side para o
    timer de inatividade em JavaScript, que pode atrasar se a aba estiver em
    segundo plano.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # request.session.session_key só ecoa o valor do cookie recebido, válido ou não.
        # A validação real só ocorre ao acessar os DADOS da sessão (.keys() força esse
        # carregamento) — se o cookie aponta para uma sessão expirada/inexistente, vem vazio.
        cookie_recebido = bool(request.COOKIES.get(settings.SESSION_COOKIE_NAME))
        sessao_invalida = (
            cookie_recebido
            and not request.session.keys()
            and not request.user.is_authenticated
        )

        response = self.get_response(request)

        # Ignora a própria rota de logout (logout_view já redireciona com o
        # aviso certo) e a rota de login (login_view já lê ?aviso= e mostra a
        # mensagem certa) — sem isso, a sessão nova/vazia criada nesse meio
        # tempo é detectada aqui de novo e duplica o aviso na página seguinte.
        eh_rota_logout = request.path == reverse('logout')
        eh_rota_login = request.path == reverse('login')

        if sessao_invalida and not eh_rota_logout and not eh_rota_login:
            messages.warning(
                request,
                "Sua sessão expirou por inatividade. Faça login novamente."
            )

        return response


class SessaoUnicaMiddleware:
    """
    Garante no máximo 1 sessão ativa por usuário ao mesmo tempo. login_view
    registra, a cada login bem-sucedido, qual session_key é a "oficial" do
    usuário (colecao_sessoes_ativas, ver obras/views.py). Aqui comparamos a
    sessão da requisição atual com essa referência: se não baterem, é porque
    um login mais novo aconteceu em outro dispositivo/navegador — então essa
    sessão (mais antiga) é encerrada na hora, com aviso.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        eh_rota_logout = request.path == reverse('logout')
        eh_rota_login = request.path == reverse('login')

        if request.user.is_authenticated and not eh_rota_logout and not eh_rota_login:
            # Import local pra evitar qualquer risco de import circular na
            # inicialização do Django (obras.views monta a conexão Mongo).
            from obras.views import colecao_sessoes_ativas

            sessao_atual = request.session.session_key
            doc = colecao_sessoes_ativas.find_one({'_id': request.user.pk})

            if doc and sessao_atual and doc.get('session_key') != sessao_atual:
                auth_logout(request)
                messages.warning(
                    request,
                    "Sua conta foi acessada em outro dispositivo. Esta sessão foi encerrada."
                )

                # A maioria das navegações no sistema é via HTMX (hx-get/hx-post),
                # que intercepta um redirect normal e tentaria encaixar a página
                # de login (que é uma página inteira, com nav/rodapé) dentro do
                # fragmento já carregado — quebrando o layout e "escondendo" o
                # aviso. HX-Redirect instrui o htmx a fazer uma navegação cheia
                # do navegador em vez de tentar trocar só o fragmento.
                if request.headers.get('HX-Request'):
                    resposta = HttpResponse(status=204)
                    resposta['HX-Redirect'] = reverse('login')
                    return resposta

                return redirect('login')

        return self.get_response(request)
