# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Django web app ("Painel de Gestão de Obras") for managing public works (obras) and employees (funcionários) for a municipality. There is no `requirements.txt` or README — dependencies are tracked only by what's installed in the active Python environment.

## Commands

```powershell
python manage.py runserver        # run dev server
python manage.py makemigrations   # create migrations (rarely needed — see Data layer below)
python manage.py migrate          # apply migrations (only affects Django's own auth/sessions tables)
python manage.py test obras       # run tests (obras/tests.py is currently empty)
python manage.py createsuperuser  # create a Django admin user
```

There is no linter or formatter configured.

## Data layer — split across two databases

This codebase uses **two separate, unrelated data stores**, and it's the single most important thing to understand before touching any view:

1. **MongoDB via `django_mongodb_backend`** (configured as the Django `DATABASES['default']` in [core/settings.py](core/settings.py)) — backs Django's own built-in apps: `auth` (User/login), `sessions`, `contenttypes`, `admin`. The `obras` app has no models ([obras/models.py](obras/models.py) is empty) and is not part of this ORM-backed database.
2. **Raw `pymongo.MongoClient`** connections, opened ad hoc inside individual view functions in [obras/views.py](obras/views.py), talking to the *same* Mongo cluster but using direct collection reads/writes (`Banco_Projeto.Banco_Obras`, `Banco_Projeto.Banco_funcionarios`, `Banco_Projeto.Banco_Timelapse`). This is where all "obra" and "funcionário" business data actually lives — completely outside the Django ORM/migrations system.

So: **Django `User` objects are the auth/login records (keyed by CPF as `username`)**, while the actual employee profile data (name, RG, birthdate, etc.) is a *separate* document in `Banco_funcionarios` linked only by matching `CPF`. Any change to employee or work data must keep both in sync manually (see `cadastro_funcionario`, `salva_edicao_funcionario` in views.py for the pattern).

Mongo connection strings and Cloudinary/Google credentials are hardcoded directly in [core/settings.py](core/settings.py) and [obras/views.py](obras/views.py) (one `MongoClient(...)` call per view, not a shared client) — this is legacy/prototype-style code, not following 12-factor config.

## External integrations

- **Cloudinary** — image uploads for obra photos (`cloudinary.uploader.upload`), configured in [core/settings.py](core/settings.py).
- **Google Sheets via `gspread`** — every obra create/update also mirrors into a Google Sheet ("Data base OBRAS DE ITABIANA") using service-account credentials in [credenciais.json](credenciais.json). See `salvar_no_google_sheets` / `atualizar_no_google_sheets` in [obras/views.py](obras/views.py). Sheet writes are best-effort: failures there are caught and just downgrade the success message, they never roll back the Mongo write.
- **NTP** (`ntplib`) — `pegar_ano_google()` tries to fetch the real-world year from a public NTP server (for generating sequential obra IDs like `12026`), falling back to the local system clock on failure.

## Request flow / templating pattern

Templates aren't full pages per route — there's one [obras/templates/base.html](obras/templates/base.html) shell with a `#conteudo-dinamico` container, and the nav uses **HTMX** (`hx-get`/`hx-target="#conteudo-dinamico"`/`hx-push-url`) to swap in partial templates without full reloads. Every view in [obras/views.py](obras/views.py) therefore branches on `request.headers.get('HX-Request')`:
- If HTMX request → render just the partial template (e.g. `cadastro_obras.html`).
- If direct/full-page load (or F5) → render `index.html` with `template_meio` set to the partial name, so the shell + partial render together.

Keep this dual-render branching when adding new views that should participate in the HTMX nav.

## Access control

There's no Django `@login_required`/`@permission_required` decorator usage; access checks are done manually inline at the top of each view (`if not request.user.is_authenticated`, `if not request.user.is_staff`), redirecting to `login` or `inicio` with a `messages.error/warning` flash. A `staff_required` decorator exists in views.py but most views inline the same checks instead of using it — follow the existing inline pattern for consistency unless refactoring deliberately.

Login is by CPF: `login_view` strips `.`/`-` from the submitted `username` field before calling `authenticate()`, since Django `User.username` stores the bare CPF digits.

## Date handling

All dates are stored in Mongo as `DD/MM/AAAA` strings (Brazilian format) or `—` for empty, while HTML date inputs use `AAAA-MM-DD`. Views convert back and forth manually (`formatar_data_br`, `preparar_data_para_input` helpers, redefined locally in several views rather than shared). `data_e_valida()` at the top of [obras/views.py](obras/views.py) is the shared validator (checks year >= 1900, future-birthdate guard, etc.) — reuse it rather than re-validating dates differently.
