# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Django web app ("Painel de Gestão de Obras") for managing public works (obras) and employees (funcionários) for a municipality. Deployed on Render (free tier) from the `ViniciusHproj/projetoitabaiana` GitHub repo, auto-deploying on push to `main`.

## Commands

```powershell
python manage.py runserver        # run dev server
python manage.py makemigrations   # create migrations (rarely needed — see Data layer below)
python manage.py migrate          # currently BROKEN — see "Known issues" below
python manage.py test obras       # run tests (obras/tests.py is currently empty — no automated tests exist yet)
python manage.py createsuperuser  # create a Django admin user
python manage.py collectstatic --noinput  # required before deploy (WhiteNoise serves the result)
```

There is no linter or formatter configured.

## Configuration / secrets

All secrets live in `.env` (gitignored, loaded via `python-dotenv` in `core/settings.py`) — never hardcode credentials back into `settings.py` or `views.py`. See `.env.example` for the required keys. In production (Render), `GOOGLE_CREDENTIALS_JSON` holds the full contents of the Google service-account JSON as an env var; `core/settings.py` writes it out to `credenciais.json` on startup if that file doesn't already exist locally.

## Known issues

- **`manage.py migrate` is broken**: `django_mongodb_backend` + Django 6.0.4 raises `TypeError: cannot use '__fake__.ContentType' as a set element` when creating permissions. Doesn't block normal usage (auth/session collections already exist in Mongo), but blocks bootstrapping a brand-new database from scratch. Needs a version pin or signal-handling fix.
- No automated test suite — changes are currently verified by ad hoc manual scripts, not committed to the repo.

## Data layer — split across two databases

This codebase uses **two separate, unrelated data stores**, and it's the single most important thing to understand before touching any view:

1. **MongoDB via `django_mongodb_backend`** (configured as the Django `DATABASES['default']` in [core/settings.py](core/settings.py)) — backs Django's own built-in apps: `auth` (User/login), `sessions`, `contenttypes`, `admin`. The `obras` app has no models ([obras/models.py](obras/models.py) is empty) and is not part of this ORM-backed database.
2. **Raw `pymongo.MongoClient`** — a single shared client/collections (`colecao_obras`, `colecao_funcionarios`, `colecao_timelapse`) created once at module load in [obras/views.py](obras/views.py) (`connect=False` so a transient Mongo outage doesn't crash app startup), talking to the *same* Mongo cluster but using direct collection reads/writes (`Banco_Projeto.Banco_Obras`, `Banco_Projeto.Banco_funcionarios`, `Banco_Projeto.Banco_Timelapse`). This is where all "obra" and "funcionário" business data actually lives — completely outside the Django ORM/migrations system. Reuse these module-level collections rather than creating new `MongoClient` instances per view/request.

So: **Django `User` objects are the auth/login records (keyed by CPF as `username`)**, while the actual employee profile data (name, RG, birthdate, etc.) is a *separate* document in `Banco_funcionarios` linked only by matching `CPF`. Any change to employee or work data must keep both in sync manually (see `cadastro_funcionario`, `salva_edicao_funcionario` in views.py for the pattern).

`colecao_obras` has a unique index on `ID_OBRA` (created idempotently at module load). `cadastro_obras`'s ID-generation loop relies on this: it computes the next number from a real `count_documents` count (so deleting an obra frees up that number again — no permanent gaps), then retries the insert on `DuplicateKeyError` if two requests race for the same number. Do not replace this with a separate incrementing-counter collection — that trade-off (no gaps vs. race-safety) was deliberately rejected in favor of the count+retry approach.

## External integrations

- **Cloudinary** — image uploads for obra photos (`cloudinary.uploader.upload`), configured from env vars in [core/settings.py](core/settings.py).
- **Google Sheets via `gspread`** — every obra create/update also mirrors into a Google Sheet ("Data base OBRAS DE ITABIANA") using service-account credentials (`credenciais.json` / `GOOGLE_CREDENTIALS_JSON`). See `salvar_no_google_sheets` / `atualizar_no_google_sheets` in [obras/views.py](obras/views.py). These calls run in a background daemon thread (`_disparar_em_background`) so the Sheets API latency never blocks the HTTP response — failures are logged via `logger.exception`, not surfaced to the user, and never roll back the Mongo write. There's no retry/queue if the background write fails.
- **NTP** (`ntplib`) — `pegar_ano_google()` tries to fetch the real-world year from a public NTP server (for generating sequential obra IDs like `12026`), falling back to the local system clock on failure.

## Caching

`lista_obras` caches its paginated results via `django.core.cache` (default `LocMemCache`, 120s TTL) to avoid re-querying Mongo on every page view. Cache keys are versioned (`CACHE_KEY_VERSAO_OBRAS` + `_bump_cache_obras()`), bumped on every successful `cadastro_obras`/`salva_edicao_obra`, so new/edited obras show up immediately rather than waiting for the TTL. Because `LocMemCache` is per-process, this only stays consistent as long as Render runs a single worker (`WEB_CONCURRENCY=1`) — if concurrency is ever increased, this cache needs to move to a shared backend (e.g. Redis) or the invalidation will only affect one process.

## Session / inactivity logout

There are two independent layers enforcing inactivity logout, both currently tuned to a short value for testing (search for `# TESTE` comments — meant to be reverted to 1 hour before real use):
1. **Client-side** ([obras/templates/base.html](obras/templates/base.html)): a JS timer (`tempoLimite`) resets only on real user actions (clicking a button/link/dropdown item, or a form `submit`/HTMX navigation) — NOT on mere mouse movement, scroll, or keypress. When it expires, it POSTs to `logout` with `?motivo=inatividade`.
2. **Server-side** ([obras/middleware.py](obras/middleware.py) `SessaoExpiradaMiddleware`): detects a stale/expired session cookie (one whose session data is empty despite a cookie being sent) and shows the same "logged out due to inactivity" message on the next page load — a fallback for when the JS timer is delayed by a backgrounded browser tab.

## Request flow / templating pattern

Templates aren't full pages per route — there's one [obras/templates/base.html](obras/templates/base.html) shell with a `#conteudo-dinamico` container, and the nav uses **HTMX** (`hx-get`/`hx-target="#conteudo-dinamico"`/`hx-push-url`) to swap in partial templates without full reloads. Every view in [obras/views.py](obras/views.py) therefore branches on `request.headers.get('HX-Request')`:
- If HTMX request → render just the partial template (e.g. `cadastro_obras.html`).
- If direct/full-page load (or F5) → render `index.html` with `template_meio` set to the partial name, so the shell + partial render together.

Keep this dual-render branching when adding new views that should participate in the HTMX nav.

Form submit buttons follow a consistent UX convention — keep it when adding new forms: `hx-indicator="#btn-id"` on the `<form>` pointed at the submit button's own id (a `.spinner-loading` `<div>` inside the button becomes visible and the button dims/disables via the `.htmx-request` CSS class in [style_menu.css](obras/static/style_menu.css) while the request is in flight), plus `hx-confirm="..."` on create/edit forms to require a confirmation dialog before submitting. Double-check the indicator's `id` actually matches the button — a previous mismatch in `edita_funcionario.html` silently disabled this protection.

## Access control

There's no Django `@login_required`/`@permission_required` decorator usage; access checks are done manually inline at the top of each view (`if not request.user.is_authenticated`, `if not request.user.is_staff`), redirecting to `login` or `inicio` with a `messages.error/warning` flash. A `staff_required` decorator exists in views.py but most views inline the same checks instead of using it — follow the existing inline pattern for consistency unless refactoring deliberately.

Login is by CPF: `login_view` strips `.`/`-` from the submitted `username` field before calling `authenticate()`, since Django `User.username` stores the bare CPF digits.

## Date handling

All dates are stored in Mongo as `DD/MM/AAAA` strings (Brazilian format) or `—` for empty, while HTML date inputs use `AAAA-MM-DD`. Views convert back and forth manually (`formatar_data_br`, `preparar_data_para_input` helpers, redefined locally in several views rather than shared). `data_e_valida()` at the top of [obras/views.py](obras/views.py) is the shared validator (checks year >= 1900, future-birthdate guard, etc.) — reuse it rather than re-validating dates differently.
