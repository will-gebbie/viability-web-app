from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.auth.tokens import default_token_generator
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

ROLES = ("Admin", "Scientist", "Viewer")


class Command(BaseCommand):
    help = (
        "Create an account with no password and print a one-time link the person "
        "uses to set their own. Re-run for an existing account to reissue the link."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--role", default="Viewer", choices=ROLES)
        parser.add_argument("--first", default="")
        parser.add_argument("--last", default="")

    def handle(self, email, role, first, last, **opts):
        if "@" not in email:
            raise CommandError(f"{email!r} is not an email address")

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            # Username is the full email: unique by construction, so there is no
            # collision case to handle. Logins go through EmailBackend anyway.
            user = User.objects.create(
                username=email,
                email=email,
                first_name=first,
                last_name=last,
            )
            user.set_unusable_password()  # no password exists until they pick one
            user.save()
            user.groups.add(Group.objects.get(name=role))
            self.stdout.write(f"Created {email} as {role}.")
        else:
            self.stdout.write(f"{email} already exists — reissuing link.")

        # Same token PasswordResetConfirmView validates. It hashes the current
        # password and last_login, so it dies the moment they set a password or
        # sign in — single use, no extra model needed.
        path = reverse(
            "password_reset_confirm",
            kwargs={
                "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
                "token": default_token_generator.make_token(user),
            },
        )
        days = settings.PASSWORD_RESET_TIMEOUT // 86400
        self.stdout.write(self.style.SUCCESS(f"{settings.SITE_URL}{path}"))
        self.stdout.write(f"Expires in {days} days. Send it over email or teams.")
