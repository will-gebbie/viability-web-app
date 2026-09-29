from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import INTERNAL_RESET_SESSION_TOKEN
from django.urls import include, path

# Password reset doubles as the "set your initial password" flow: `manage.py
# invite` hands out a password_reset_confirm link instead of a password.
# These views are login_not_required in Django, so LoginRequiredMiddleware lets
# them through, and their templates ship with django.contrib.admin.
urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "password/reset/",
        auth_views.PasswordResetView.as_view(),
        name="password_reset",
    ),
    path(
        "password/reset/sent/",
        auth_views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password/set/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password/done/",
        auth_views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("", include("portal.urls")),
]
