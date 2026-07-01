from django.contrib.admin.apps import AdminConfig
from django.contrib.auth.apps import AuthConfig
from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.apps import ContentTypesConfig
from django.db.models.signals import post_migrate

class MongoAdminConfig(AdminConfig):
    default_auto_field = 'django_mongodb_backend.fields.ObjectIdAutoField'

class MongoAuthConfig(AuthConfig):
    default_auto_field = 'django_mongodb_backend.fields.ObjectIdAutoField'

    def ready(self):
        super().ready()
        # django_mongodb_backend's ContentType shim produces fake, unhashable
        # instances that crash create_permissions() during `migrate` (raises
        # "TypeError: cannot use '__fake__.ContentType' as a set element") —
        # this is the "manage.py migrate is broken" issue in CLAUDE.md/README,
        # and it also blocks Django's test runner, which runs `migrate` to
        # build the test database. This app never uses Django's permission
        # system (access control is manual `is_staff`/`is_superuser` checks
        # in views.py, not `user.has_perm(...)`), so permissions are safe to
        # never auto-create — disconnecting this signal fixes both a
        # from-scratch `migrate` and `manage.py test`.
        post_migrate.disconnect(
            create_permissions,
            dispatch_uid="django.contrib.auth.management.create_permissions",
        )

class MongoContentTypesConfig(ContentTypesConfig):
    default_auto_field = 'django_mongodb_backend.fields.ObjectIdAutoField'