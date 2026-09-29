from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User


class EmailBackend(ModelBackend):
    """Authenticate with an email address instead of a username.

    AuthenticationForm always calls authenticate(username=..., password=...),
    so we treat that `username` value as an email and look the account up by it.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            return None
        # email is not unique on auth_user, so an admin can create a second
        # account sharing one. Refuse the ambiguous case rather than picking a
        # row arbitrarily — CustomUserAdmin blocks new duplicates.
        users = list(User.objects.filter(email__iexact=username)[:2])
        if len(users) != 1:
            # Same dummy hash ModelBackend runs, so a missing account takes as
            # long as a wrong password (no timing oracle for valid emails).
            User().set_password(password)
            return None
        user = users[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
