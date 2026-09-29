from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import User

# Replace the stock user admin with one that surfaces group membership.
admin.site.unregister(User)


class UniqueEmailUserForm(UserChangeForm):
    """Django's User.email isn't unique, but sign-in looks accounts up by it.

    A second account on the same address is unloggable-in (EmailBackend refuses
    the ambiguity), so block it at the point of entry instead.
    """

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if not email:
            return email
        clash = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another account already uses this email.")
        return email


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    form = UniqueEmailUserForm
    list_display = UserAdmin.list_display + ("group_names",)
    list_filter = UserAdmin.list_filter + ("groups",)

    @admin.display(description="Groups")
    def group_names(self, obj):
        names = []
        for group in obj.groups.all():
            names.append(group.name)
        return ", ".join(names)
