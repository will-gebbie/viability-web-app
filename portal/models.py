from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

# rca_id-batch_id becomes the run's URL slug, so a letter or a space there makes
# the run unroutable (<slug:run_id> won't match) rather than merely untidy.
# Kept on the model so the CSV import gets the same rule as the form.
numeric_only = RegexValidator(r"^\d+$", "Must be a whole number.")


class LiveRunManager(models.Manager):
    # Default manager: hides soft-deleted runs everywhere (lists, get_object_or_404).
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Run(models.Model):
    slug = models.SlugField(max_length=64, unique=True)  # rca_id + "-" + batch_id
    name = models.CharField(max_length=200, blank=True)
    rca_id = models.CharField(max_length=32, validators=[numeric_only])
    batch_id = models.CharField(max_length=32, validators=[numeric_only])

    STATUS_CHOICES = [("running", "Running"), ("complete", "Complete")]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="running")

    strains = models.JSONField(default=list)  # list of strain names
    comp = models.JSONField(default=list)  # matching list of composition percentages
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    od_time = models.JSONField(default=list)  # timepoints (ISO strings), shared x-axis
    od_data = models.JSONField(default=list)  # OD readings (left y-axis)
    cfu_data = models.JSONField(
        default=list
    )  # CFU readings (right y-axis), same timepoints
    oper = models.CharField(max_length=100, blank=True)  # operator
    reac = models.CharField(max_length=100, blank=True)  # reactor name or id
    media = models.CharField(max_length=100, blank=True)
    mode = models.CharField(max_length=50, blank=True)  # fed-batch, continuous, etc.
    temp = models.FloatField(null=True, blank=True)  # C
    ph = models.FloatField(null=True, blank=True)
    vol = models.FloatField(null=True, blank=True)  # L

    field_trials = models.ManyToManyField("FieldTrial", blank=True, related_name="runs")
    formulations = models.ManyToManyField(
        "Formulation", blank=True, related_name="runs"
    )
    assays = models.ManyToManyField("Assay", blank=True, related_name="runs")

    deleted_at = models.DateTimeField(null=True, blank=True)  # soft-delete marker

    objects = LiveRunManager()  # default: live runs only
    all_objects = models.Manager()  # includes soft-deleted (for trash/restore)

    class Meta:
        constraints = [
            # complete <-> has end_date; running <-> no end_date yet.
            models.CheckConstraint(
                name="run_status_end_date_consistent",
                condition=(
                    Q(status="running", end_date__isnull=True)
                    | Q(status="complete", end_date__isnull=False)
                ),
            ),
        ]

    def clean(self):
        errors = {}
        # Strain composition must add up to 100% (empty = unspecified, allowed).
        # 0.5 tolerance absorbs rounding like 33.33|33.33|33.34.
        if self.comp and abs(sum(self.comp) - 100) > 0.5:
            errors["comp"] = (
                f"Composition must add up to 100% (currently {sum(self.comp)})."
            )
        today = timezone.localdate()
        if self.start_date and self.start_date > today:
            errors["start_date"] = "Start date can't be in the future."
        if self.end_date and self.end_date > today:
            errors["end_date"] = "End date can't be in the future."
        elif self.start_date and self.end_date and self.end_date <= self.start_date:
            errors["end_date"] = "End date must be after the start date."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # slug is always rca_id-batch_id; it's the run's URL. Deriving it here
        # (rather than in every view that writes a Run) keeps the one source of
        # truth on the model.
        self.slug = f"{self.rca_id}-{self.batch_id}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.slug

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])

    @property
    def last_od(self):
        if self.od_data:
            return self.od_data[-1]
        return None

    @property
    def last_cfu(self):
        if self.cfu_data:
            return self.cfu_data[-1]
        return None

    @property
    def latest_viab(self):
        # In Python off .all() so a prefetch_related("micro_results") on the run
        # list serves the whole grid in one query instead of one per card.
        micros = self.micro_results.all()
        if not micros:
            return None
        return max(micros, key=lambda m: m.micro_date or date.min).viab


class MicroResult(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="micro_results")
    created = models.DateTimeField(auto_now_add=True)
    # A date, not a datetime: the form only ever collects one, and a DateTimeField
    # fed a date lands at naive midnight — correct only while TIME_ZONE is UTC.
    micro_date = models.DateField(null=True, blank=True)
    micro_name = models.CharField(max_length=255, blank=True)
    num_imgs = models.FloatField(null=True, blank=True)
    od = models.FloatField(null=True, blank=True)
    live = models.FloatField(null=True, blank=True)  # live %
    live_std = models.FloatField(null=True, blank=True)  # live %
    dormant = models.FloatField(null=True, blank=True)  # dormant %
    dormant_std = models.FloatField(null=True, blank=True)  # dormant %
    dying = models.FloatField(null=True, blank=True)  # dying %
    dying_std = models.FloatField(null=True, blank=True)  # dying %
    dead = models.FloatField(null=True, blank=True)  # dead %
    dead_std = models.FloatField(null=True, blank=True)  # dead %
    viab = models.FloatField(null=True, blank=True)  # live% + dormant%

    def __str__(self):
        return f"{self.run.slug} micro @ {self.micro_date:%Y-%m-%d}"


class FlowResult(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="flow_results")
    created = models.DateTimeField(auto_now_add=True)
    flow_date = models.DateField(null=True, blank=True)  # see MicroResult.micro_date
    fcs_name = models.CharField(max_length=255, blank=True)
    volume_uL = models.FloatField()
    od = models.FloatField()
    n_cells = models.IntegerField()
    cells_per_uL = models.FloatField()
    cells_per_uL_per_OD = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.run.slug} flow @ {self.flow_date:%Y-%m-%d}"


class SeqResult(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="seq_results")
    created = models.DateTimeField(auto_now_add=True)
    seq_date = models.DateField(
        null=True, blank=True
    )  # date culture was sent for sequencing
    od = models.FloatField(null=True, blank=True)  # OD of culture sent for sequencing
    strains = models.JSONField(default=list)  # list of strain names
    comp = models.JSONField(default=list)  # matching list of composition percentages

    def __str__(self):
        return f"{self.run.slug} seq @ {self.seq_date:%Y-%m-%d}"


class Formulation(models.Model):
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name


class Assay(models.Model):
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name


class FieldTrial(models.Model):
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name
