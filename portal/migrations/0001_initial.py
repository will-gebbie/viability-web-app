import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

GROUPS = ["Admin", "Scientist", "Viewer"]

# Which Run permissions each role gets. Soft delete == change_run (Scientist),
# hard delete == delete_run (Admin only).
ROLE_PERMS = {
    "Viewer": ["view_run"],
    "Scientist": ["view_run", "add_run", "change_run"],
    "Admin": ["view_run", "add_run", "change_run", "delete_run"],
}


def create_roles(apps, schema_editor):
    # On a fresh DB the Run permissions aren't created until post_migrate runs
    # at the very end, so create them now before we hand them out.
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config("portal"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for group_name in GROUPS:
        Group.objects.get_or_create(name=group_name)
    for group_name, codenames in ROLE_PERMS.items():
        group = Group.objects.get(name=group_name)
        perms = Permission.objects.filter(
            content_type__app_label="portal",
            content_type__model="run",
            codename__in=codenames,
        )
        group.permissions.set(perms)


def delete_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=GROUPS).delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name='Assay',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
            ],
        ),
        migrations.CreateModel(
            name='FieldTrial',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
            ],
        ),
        migrations.CreateModel(
            name='Formulation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
            ],
        ),
        migrations.CreateModel(
            name='Run',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField(max_length=64, unique=True)),
                ('name', models.CharField(blank=True, max_length=200)),
                ('rca_id', models.CharField(max_length=32, validators=[django.core.validators.RegexValidator('^\\d+$', 'Must be a whole number.')])),
                ('batch_id', models.CharField(max_length=32, validators=[django.core.validators.RegexValidator('^\\d+$', 'Must be a whole number.')])),
                ('status', models.CharField(choices=[('running', 'Running'), ('complete', 'Complete')], default='running', max_length=16)),
                ('strains', models.JSONField(default=list)),
                ('comp', models.JSONField(default=list)),
                ('start_date', models.DateField(blank=True, null=True)),
                ('end_date', models.DateField(blank=True, null=True)),
                ('od_time', models.JSONField(default=list)),
                ('od_data', models.JSONField(default=list)),
                ('cfu_data', models.JSONField(default=list)),
                ('oper', models.CharField(blank=True, max_length=100)),
                ('reac', models.CharField(blank=True, max_length=100)),
                ('media', models.CharField(blank=True, max_length=100)),
                ('mode', models.CharField(blank=True, max_length=50)),
                ('temp', models.FloatField(blank=True, null=True)),
                ('ph', models.FloatField(blank=True, null=True)),
                ('vol', models.FloatField(blank=True, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('assays', models.ManyToManyField(blank=True, related_name='runs', to='portal.assay')),
                ('field_trials', models.ManyToManyField(blank=True, related_name='runs', to='portal.fieldtrial')),
                ('formulations', models.ManyToManyField(blank=True, related_name='runs', to='portal.formulation')),
            ],
        ),
        migrations.CreateModel(
            name='MicroResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('micro_date', models.DateField(blank=True, null=True)),
                ('micro_name', models.CharField(blank=True, max_length=255)),
                ('num_imgs', models.FloatField(blank=True, null=True)),
                ('od', models.FloatField(blank=True, null=True)),
                ('live', models.FloatField(blank=True, null=True)),
                ('live_std', models.FloatField(blank=True, null=True)),
                ('dormant', models.FloatField(blank=True, null=True)),
                ('dormant_std', models.FloatField(blank=True, null=True)),
                ('dying', models.FloatField(blank=True, null=True)),
                ('dying_std', models.FloatField(blank=True, null=True)),
                ('dead', models.FloatField(blank=True, null=True)),
                ('dead_std', models.FloatField(blank=True, null=True)),
                ('viab', models.FloatField(blank=True, null=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='micro_results', to='portal.run')),
            ],
        ),
        migrations.CreateModel(
            name='FlowResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('flow_date', models.DateField(blank=True, null=True)),
                ('fcs_name', models.CharField(blank=True, max_length=255)),
                ('volume_uL', models.FloatField()),
                ('od', models.FloatField()),
                ('n_cells', models.IntegerField()),
                ('cells_per_uL', models.FloatField()),
                ('cells_per_uL_per_OD', models.FloatField(blank=True, null=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='flow_results', to='portal.run')),
            ],
        ),
        migrations.CreateModel(
            name='SeqResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('seq_date', models.DateField(blank=True, null=True)),
                ('od', models.FloatField(blank=True, null=True)),
                ('strains', models.JSONField(default=list)),
                ('comp', models.JSONField(default=list)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='seq_results', to='portal.run')),
            ],
        ),
        migrations.AddConstraint(
            model_name='run',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('end_date__isnull', True), ('status', 'running')), models.Q(('end_date__isnull', False), ('status', 'complete')), _connector='OR'), name='run_status_end_date_consistent'),
        ),
        migrations.RunPython(create_roles, delete_roles),
    ]
