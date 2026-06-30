from django.conf import settings
from django.contrib import messages
from django.urls import reverse


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
                "⏱️ Sua sessão expirou por inatividade. Faça login novamente."
            )

        return response
