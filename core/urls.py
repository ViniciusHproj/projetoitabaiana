from django.contrib import admin
from django.urls import path
from obras.views import pagina_inicial, cadastro_obras, index, cadastro_funcionario, lista_obras, busca_atualiza_obra, busca_atualiza_funcionario, salva_edicao_obra, salva_edicao_funcionario, login_view, logout_view, galeria_obra, deletar_obra, deletar_funcionario, dashboard_publico, zona_admin, alterar_cargo_funcionario
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', pagina_inicial, name='raizhome'),
    path('cadastro-obras/', cadastro_obras, name='Cadastro-Obras'),
    path('inicio/', index, name='inicio'),
    # Rota de Login: Usamos a classe pronta do Django e apontamos para o seu HTML
    path('login/', login_view, name='login'),
    # Rota de Logout: Para o usuário poder sair do sistema
    path('logout/', logout_view, name='logout'),
    path('cadastro-funcionario/', cadastro_funcionario, name='cadastro_funcionario'),
    path('lista-obras/', lista_obras, name='lista_obras'),
    path('atualizar-obra/', busca_atualiza_obra, name='busca_atualiza_obra'),
    path('salvar-edicao-obra/', salva_edicao_obra, name='salva_edicao_obra'),
    path('atualizar-funcionario/', busca_atualiza_funcionario, name='busca_atualiza_funcionario'),
    path('salvar-edicao-funcionario/', salva_edicao_funcionario, name='salva_edicao_funcionario'),
    path('galeria/<str:id_obra>/', galeria_obra, name='galeria_obra'),
    path('deletar-obra/', deletar_obra, name='deletar_obra'),
    path('dashboard-obras/', dashboard_publico, name='dashboard_publico'),
    path('zona-admin/', zona_admin, name='zona_admin'),
    path('alterar-cargo/', alterar_cargo_funcionario, name='alterar_cargo_funcionario'),
    path('deletar-funcionario/', deletar_funcionario, name='deletar_funcionario'),
]