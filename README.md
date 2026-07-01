# Painel de Gestão de Obras

Sistema web para gestão de obras públicas e funcionários de uma prefeitura/município, com cadastro, edição, acompanhamento e galeria de fotos de obras, controle de acesso por nível de usuário e mirror automático dos dados para uma planilha do Google Sheets.

Construído com **Django**, **MongoDB** (via `django-mongodb-backend` para autenticação/sessões e `pymongo` direto para os dados de negócio), **HTMX** para navegação dinâmica sem recarregar a página, e deploy automatizado no **Render**.

---

## Funcionalidades

- **Login por CPF** com rate-limit anti-força-bruta (bloqueio temporário por IP e por conta após tentativas seguidas erradas).
- **Sessão única por usuário** — logar em um novo dispositivo encerra automaticamente qualquer sessão anterior daquela conta.
- **Cadastro e edição de obras**: tipo, situação, valor, datas (início/conclusão/finalização), empresa contratada (com validação de CNPJ), endereço, galeria de fotos (upload para Cloudinary) e geração automática de ID sequencial por ano.
- **Cadastro e edição de funcionários**: dados pessoais, RG, CPF (com validação de dígito verificador), nível de acesso (Comum/Supervisor), tudo sincronizado entre o usuário de autenticação (Django) e o perfil de dados (MongoDB).
- **Controle de acesso por papel**: funcionários comuns podem cadastrar/editar obras; apenas supervisores podem cadastrar/editar outros funcionários.
- **Dashboard público** de acompanhamento de obras (lista paginada, com cache) — não exige login.
- **Galeria/timelapse de fotos** por obra, com histórico de quando cada foto foi adicionada.
- **Sincronização automática com Google Sheets** a cada cadastro/edição de obra (em segundo plano, não bloqueia a resposta ao usuário).
- **Logout automático por inatividade** (30 minutos), com aviso tanto no navegador (JS) quanto no servidor (fallback).
- **Tema claro/escuro**, com preferência salva no navegador.
- Proteção contra reenvio de formulário no F5 (padrão Post/Redirect/Get) em todos os formulários de cadastro/edição.

---

## Stack técnica

| Camada | Tecnologia |
|---|---|
| Backend | Django 6.0 |
| Banco (auth/sessões) | MongoDB via `django-mongodb-backend` |
| Banco (dados de negócio) | MongoDB via `pymongo` (acesso direto, fora do ORM) |
| Frontend dinâmico | HTMX (navegação parcial sem reload completo) |
| Upload de imagens | Cloudinary |
| Planilha espelhada | Google Sheets (via `gspread` + conta de serviço) |
| Servidor de produção | Gunicorn + WhiteNoise (arquivos estáticos) |
| Hospedagem | Render (deploy automático a partir da branch `main`) |

---

## Arquitetura — dois bancos de dados

Este é o ponto mais importante para entender o projeto: ele usa **dois "bancos" logicamente separados, na mesma instância do MongoDB**:

1. **Django ORM** (`django-mongodb-backend`) — só para os apps internos do Django: `auth` (usuários/login), `sessions`, `contenttypes`, `admin`. O app `obras` **não tem models** — não passa pelo ORM.
2. **`pymongo` direto** — coleções de negócio (`Banco_Obras`, `Banco_funcionarios`, `Banco_Timelapse`, `Banco_SegurancaLogin`, `Banco_SessoesAtivas`), lidas/escritas manualmente em `obras/views.py`, totalmente fora do sistema de migrações do Django.

Ou seja: o **login** (CPF como `username`) é um registro do Django `User`; os **dados do funcionário** (nome, RG, data de nascimento, etc.) ficam em um documento separado no Mongo, ligado só pelo CPF em comum. Qualquer alteração em dado de funcionário precisa manter os dois sincronizados manualmente.

---

## Estrutura do projeto

```
core/                  # configuração do projeto Django (settings, urls, wsgi)
obras/
  views.py             # toda a lógica de negócio (cadastro, login, validações, etc.)
  middleware.py         # sessão expirada por inatividade + sessão única por usuário
  utils.py              # helpers de formatação de data compartilhados
  templates/             # templates HTML (HTMX), um por tela/fragmento
  static/                # CSS e JS (style_nav.css, style_obras.css, script_menu.js)
manage.py
requirements.txt
Procfile               # comando de start usado pelo Render (gunicorn)
.env.example           # lista de variáveis de ambiente necessárias
```

---

## Rodando localmente

### Pré-requisitos
- Python 3.x
- Acesso a um cluster MongoDB (Atlas ou local)
- Conta no Cloudinary (para upload de fotos)
- Credenciais de uma conta de serviço do Google (para o Sheets, opcional em dev)

### Passos

```powershell
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Copiar o template de variáveis de ambiente e preencher com seus valores
copy .env.example .env

# 3. Rodar o servidor de desenvolvimento
python manage.py runserver
```

O projeto estará disponível em `http://127.0.0.1:8000/`.

### Criar um usuário administrador

```powershell
python manage.py createsuperuser
```

---

## Variáveis de ambiente

Veja `.env.example` para a lista completa. As principais:

| Variável | Descrição |
|---|---|
| `DJANGO_SECRET_KEY` | Chave secreta do Django — única e forte em produção, nunca reaproveitada do `.env` local |
| `DJANGO_DEBUG` | `True` em dev, **sempre `False`** em produção |
| `DJANGO_ALLOWED_HOSTS` | Domínios permitidos (separados por vírgula) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | URL completa (com `https://`) do domínio de produção |
| `MONGODB_URI` / `MONGODB_DB_NAME` | Conexão com o MongoDB |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Credenciais do Cloudinary |
| `GOOGLE_SHEETS_CREDENTIALS_FILE` | Caminho do arquivo de credenciais da conta de serviço (`credenciais.json`) |
| `GOOGLE_SHEETS_SPREADSHEET_NAME` | Nome da planilha do Google Sheets espelhada |
| `GOOGLE_CREDENTIALS_JSON` | Conteúdo inteiro do `credenciais.json` em uma linha — usado **só em produção** (Render), onde o arquivo não é versionado |

Em produção (Render), quando `DEBUG=False`, configurações adicionais de segurança são ativadas automaticamente: redirecionamento forçado para HTTPS, cookies de sessão/CSRF marcados como seguros, e HSTS.

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

- **Rate-limit de login** persistido no MongoDB (sobrevive a reinícios/deploys do Render) — bloqueio temporário por IP e por CPF após tentativas seguidas erradas, com janela deslizante. O IP do cliente é lido a partir do **último** valor de `X-Forwarded-For` (o escrito pelo proxy confiável do Render), nunca o primeiro — o primeiro valor vem do próprio cliente e poderia ser forjado para burlar o bloqueio por IP.
- **Sessão única por usuário** — um novo login em outro dispositivo encerra a sessão anterior automaticamente.
- **Validação de CPF e CNPJ** com cálculo real de dígito verificador (não só formato).
- **Validação de força de senha** (`AUTH_PASSWORD_VALIDATORS`) aplicada explicitamente no cadastro e na troca de senha de funcionários — Django não faz isso automaticamente fora dos formulários prontos dele.
- **Proteção contra duplo-submit** em todos os formulários de cadastro/edição (trava temporária + token de uso único).
- **HTTPS forçado, cookies seguros e HSTS** em produção.
- Controle de acesso por papel (`is_staff`) verificado em cada view sensível (cadastro/edição de funcionários é restrito a supervisores; cadastro/edição de obras é liberado para qualquer funcionário autenticado, por decisão de negócio).

---

## Testes automatizados

```powershell
python manage.py test obras
```

A suíte cobre validadores (CPF/CNPJ/RG/datas/valor), a correção de segurança do IP via `X-Forwarded-For`, rate-limit de login, sessão única por usuário, e os fluxos de cadastro/edição de obras e funcionários. Requer conectividade real com o MongoDB do `.env` — os testes usam um banco de teste à parte no mesmo cluster (`{MONGODB_DB_NAME}_teste`), criado e destruído automaticamente a cada execução; nada é gravado no banco de produção. Upload de fotos (Cloudinary) é mockado, então não precisa de credenciais reais do Cloudinary para rodar os testes.

---

## Problemas conhecidos

- O cache de listagem de obras (`LocMemCache`) é por processo — o Render precisa rodar com 1 único worker (`WEB_CONCURRENCY=1`) para a invalidação de cache funcionar corretamente.
- A sincronização com Google Sheets roda em segundo plano com até 3 tentativas (5s, 15s de espera); se todas falharem, a obra fica salva no Mongo mas não aparece na planilha até a próxima edição.
