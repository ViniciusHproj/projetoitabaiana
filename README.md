# Painel de Gestão de Obras

Sistema web para gestão de obras públicas e funcionários de uma prefeitura/município, com cadastro, edição, acompanhamento e galeria de fotos de obras, controle de acesso por nível de usuário e mirror automático dos dados para uma planilha do Google Sheets.

Construído com **Django 6**, **MongoDB** (via `django-mongodb-backend` para autenticação/sessões e `pymongo` direto para os dados de negócio), **HTMX** para navegação dinâmica sem recarregar a página, e deploy automatizado no **Render**.

---

## Funcionalidades

- **Dashboard público** de acompanhamento de obras (lista paginada, com cache, sem exigir login) — vitrine do município para qualquer cidadão.
- **Galeria de fotos por obra**, com histórico de quando cada foto foi adicionada (timelapse).
- **Login por CPF** com rate-limit anti-força-bruta (bloqueio temporário por IP e por conta após tentativas seguidas erradas, persistido no MongoDB — sobrevive a reinícios do Render).
- **Sessão única por usuário** — logar em um novo dispositivo encerra automaticamente qualquer sessão anterior daquela conta.
- **Cadastro e edição de obras**: tipo, situação, tipo de execução, valor, datas (início/conclusão prevista/finalização), empresa contratada (com validação de CNPJ), endereço, galeria de fotos (upload para Cloudinary, até 10 fotos por envio) e geração automática de ID sequencial por ano.
- **Cadastro e edição de funcionários**: dados pessoais, RG, CPF (com validação de dígito verificador), função (Comum/Supervisor), tudo sincronizado entre o usuário de autenticação (Django) e o perfil de dados (MongoDB).
- **Controle de acesso por papel**: funcionários comuns podem cadastrar/editar obras; apenas supervisores podem cadastrar/editar outros funcionários.
- **Sincronização automática com Google Sheets** a cada cadastro/edição de obra (em segundo plano, não bloqueia a resposta ao usuário; retenta até 3x em caso de falha transiente).
- **Logout automático por inatividade** (30 minutos), com dupla camada: timer JS no navegador + middleware server-side de fallback.
- **Tema claro/escuro**, com preferência salva no navegador (`localStorage`).
- **Proteção contra duplo-submit** em todos os formulários de cadastro/edição: trava temporária por usuário + token UUID de uso único por renderização de form.

---

## Stack técnica

| Camada | Tecnologia |
|---|---|
| Backend | Django 6.0.4 |
| Banco (auth/sessões) | MongoDB via `django-mongodb-backend` |
| Banco (dados de negócio) | MongoDB via `pymongo` (acesso direto, fora do ORM) |
| Frontend dinâmico | HTMX 1.9.10 (navegação parcial sem reload completo) |
| Upload de imagens | Cloudinary |
| Planilha espelhada | Google Sheets (via `gspread` + conta de serviço Google) |
| Servidor de produção | Gunicorn + WhiteNoise (arquivos estáticos) |
| Hospedagem | Render (deploy automático a partir da branch `main`) |

---

## Arquitetura — dois bancos de dados

Este é o ponto mais importante para entender o projeto: ele usa **dois "bancos" logicamente separados, na mesma instância do MongoDB**:

1. **Django ORM** (`django-mongodb-backend`) — só para os apps internos do Django: `auth` (usuários/login), `sessions`, `contenttypes`, `admin`. O app `obras` **não tem models** — não passa pelo ORM.
2. **`pymongo` direto** — coleções de negócio (`Banco_Obras`, `Banco_funcionarios`, `Banco_Timelapse`, `Banco_SegurancaLogin`, `Banco_SessoesAtivas`), lidas/escritas manualmente em `obras/views.py`, totalmente fora do sistema de migrações do Django.

Ou seja: o **login** (CPF como `username`) é um registro do Django `User`; os **dados do funcionário** (nome, RG, data de nascimento, função, etc.) ficam em um documento separado no Mongo, ligado só pelo CPF em comum. Qualquer alteração em dado de funcionário precisa manter os dois sincronizados manualmente.

---

## Estrutura do projeto

```
core/                  # configuração do projeto Django (settings, urls, wsgi, apps)
obras/
  views.py             # toda a lógica de negócio (cadastro, login, validações, etc.)
  middleware.py        # sessão expirada por inatividade + sessão única por usuário
  utils.py             # helpers de formatação de data compartilhados
  tests.py             # suite de 36 testes automatizados
  templates/           # templates HTML (HTMX), um por tela/fragmento
  static/              # CSS e JS (style_nav.css, style_obras.css, script_menu.js)
manage.py
requirements.txt
Procfile               # comando de start usado pelo Render (gunicorn + collectstatic)
.env.example           # lista de variáveis de ambiente necessárias
```

---

## Rodando localmente

### Pré-requisitos
- Python 3.x
- Acesso a um cluster MongoDB (Atlas ou local)
- Conta no Cloudinary (para upload de fotos)
- Credenciais de uma conta de serviço do Google (para o Sheets — opcional em dev)

### Passos

```powershell
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Copiar o template de variáveis de ambiente e preencher com seus valores
copy .env.example .env

# 3. Criar as coleções/índices do MongoDB
python manage.py migrate

# 4. Rodar o servidor de desenvolvimento
python manage.py runserver
```

O projeto estará disponível em `http://127.0.0.1:8000/`.

### Criar um usuário administrador

```powershell
python manage.py createsuperuser
```

> **Atenção**: contas criadas via `createsuperuser` não têm `first_name` preenchido — o sistema usa `username` (CPF) como fallback na saudação de login. Para contas com nome correto, use o Cadastro de Funcionário pela interface.

---

## Variáveis de ambiente

Veja `.env.example` para a lista completa. As principais:

| Variável | Descrição |
|---|---|
| `DJANGO_SECRET_KEY` | Chave secreta do Django — única e forte em produção, nunca reutilizada do `.env` local |
| `DJANGO_DEBUG` | `True` em dev, **sempre `False`** em produção |
| `DJANGO_ALLOWED_HOSTS` | Domínios permitidos (separados por vírgula) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | URL completa (com `https://`) do domínio de produção |
| `MONGODB_URI` / `MONGODB_DB_NAME` | Conexão com o MongoDB |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Credenciais do Cloudinary |
| `GOOGLE_SHEETS_CREDENTIALS_FILE` | Caminho do arquivo de credenciais da conta de serviço (`credenciais.json`) |
| `GOOGLE_SHEETS_SPREADSHEET_NAME` | Nome da planilha do Google Sheets espelhada |
| `GOOGLE_CREDENTIALS_JSON` | Conteúdo inteiro do `credenciais.json` em uma linha — usado **só em produção** (Render), onde o arquivo não é versionado |

Em produção (Render), quando `DEBUG=False`, configurações adicionais de segurança são ativadas automaticamente: redirecionamento forçado para HTTPS, cookies de sessão/CSRF marcados como seguros, e HSTS (1 ano).

---

## Deploy

O deploy é automático: qualquer push na branch `main` do GitHub dispara um novo build/deploy no Render (`Procfile` roda `collectstatic` e sobe com `gunicorn`). O desenvolvimento normalmente acontece na branch `dev`, mergeada para `main` quando pronta para publicar:

```powershell
git checkout main
git merge dev
git push origin main
```

---

## Segurança implementada

- **Rate-limit de login** persistido no MongoDB (sobrevive a reinícios/deploys do Render) — bloqueio temporário por IP e por CPF após tentativas seguidas erradas, com janela deslizante de 15 minutos. O IP do cliente é lido a partir do **último** valor de `X-Forwarded-For` (o escrito pelo proxy confiável do Render), nunca o primeiro — o primeiro vem do próprio cliente e poderia ser forjado para burlar o bloqueio por IP.
- **Sessão única por usuário** — um novo login em outro dispositivo encerra a sessão anterior automaticamente, com feedback via `HX-Redirect` para preservar o layout HTMX.
- **Validação de CPF e CNPJ** com cálculo real de dígito verificador (não só formato).
- **Validação de força de senha** (`AUTH_PASSWORD_VALIDATORS`) aplicada explicitamente no cadastro e na troca de senha de funcionários — Django não faz isso automaticamente fora dos formulários prontos dele.
- **Proteção contra duplo-submit** em todos os formulários de cadastro/edição: trava temporária de debounce (4s funcionário / 12s obra) + token UUID de uso único por renderização.
- **Token de idempotência** (campo oculto `form_token`) em todos os forms de criação/edição — POST sem token é rejeitado (fail-secure).
- **CSRF** em todos os formulários POST (incluindo logout).
- **Logout POST-only** — previne CSRF-logout via `<img src="/logout/">` de outro site.
- **Proteção contra open-redirect** em `?next=` no login, validado com `url_has_allowed_host_and_scheme()`.
- **HTTPS forçado, cookies seguros e HSTS** em produção.
- **Controle de acesso por papel** (`is_staff`) verificado em cada view sensível — cadastro/edição de funcionários é restrito a supervisores; cadastro/edição de obras é liberado para qualquer funcionário autenticado (por decisão de negócio).
- **Rollback manual** de dados do Django em caso de falha no MongoDB em `salva_edicao_funcionario`, para evitar divergência entre os dois bancos.

---

## Testes automatizados

```powershell
python manage.py test obras
```

A suite (36 testes) cobre: validadores de CPF/CNPJ/RG/datas/valor, a correção de segurança do IP via `X-Forwarded-For`, rate-limit de login, sessão única por usuário, e os fluxos de cadastro/edição de obras e funcionários (incluindo idempotência por `form_token`).

Requer conectividade real com o MongoDB do `.env` — os testes usam um banco de teste à parte no mesmo cluster (`{MONGODB_DB_NAME}_teste`), criado e destruído automaticamente a cada execução; nada é gravado no banco de produção. Upload de fotos (Cloudinary) é mockado, então não precisa de credenciais reais do Cloudinary para rodar.

---

## Problemas conhecidos / limitações

- O cache de listagem de obras (`LocMemCache`) é por processo — o Render precisa rodar com 1 único worker (`WEB_CONCURRENCY=1`) para a invalidação de cache funcionar corretamente entre requests.
- A sincronização com Google Sheets roda em segundo plano com até 3 tentativas (espera de 5s e 15s entre elas); se todas falharem, a obra fica salva no Mongo mas não aparece na planilha até a próxima edição. Obras que nunca foram sincronizadas (ex: criadas durante uma falha do Sheets) não disparam retentativas ao serem editadas — apenas um aviso é logado.
- Fotos antigas no Cloudinary não são deletadas quando a capa de uma obra é substituída — consomem cota do plano ao longo do tempo.
- Não há fila durável para os jobs de background (Google Sheets, Cloudinary timelapse) — falhas persistentes após 3 tentativas são apenas logadas e perdidas até a próxima interação manual com aquela obra.
