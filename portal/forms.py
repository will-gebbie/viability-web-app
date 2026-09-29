import re

from django import forms

from .models import Run

# Strain names become multipart field names AND per-strain filenames on the
# aligner service. Left unchecked, a "/" or ".." is path traversal and a quote
# or CRLF is header injection, so restrict them at the trust boundary to
# letters, digits, and a few separators. (The service basenames defensively too.)
STRAIN_NAME_RE = re.compile(r"^[\w .+-]+$")

# Shared input styling (matches the hand-styled panel look).
INPUT = "padding:9px 11px;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;width:100%;min-width:0"

# Per-file upload ceiling — guards the analysis services from OOM on huge files.
MAX_UPLOAD_MB = 500
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Nanopore read sets are routinely 1-2 GB gzipped, so they get their own ceiling.
MAX_READS_MB = 3000

# CSVs (OD upload, run import) are hand-maintained spreadsheets — kilobytes. The
# views .read() them whole into memory, so cap them well below the file ceiling.
MAX_CSV_MB = 10


def _check_size(f, limit_mb=MAX_UPLOAD_MB):
    if f.size > limit_mb * 1024 * 1024:
        raise forms.ValidationError(f"{f.name}: exceeds the {limit_mb} MB limit.")


class FlowCytometryForm(forms.Form):
    fcs_file = forms.FileField(
        widget=forms.ClearableFileInput(attrs={"accept": ".fcs", "style": INPUT})
    )
    volume = forms.FloatField(
        min_value=0,
        widget=forms.NumberInput(
            attrs={"style": INPUT, "step": "any", "placeholder": "25.0"}
        ),
    )
    od = forms.FloatField(
        min_value=0,
        widget=forms.NumberInput(
            attrs={"style": INPUT, "step": "any", "placeholder": "0.11"}
        ),
    )
    flow_date = forms.DateField(
        widget=forms.DateInput(attrs={"style": INPUT, "type": "date"})
    )

    def clean_fcs_file(self):
        f = self.cleaned_data["fcs_file"]
        if not f.name.lower().endswith(".fcs"):
            raise forms.ValidationError("File must be a .fcs file")
        _check_size(f)
        return f


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if not isinstance(data, (list, tuple)):
            return single(data, initial)
        result = []
        for d in data:
            result.append(single(d, initial))
        return result


class MicroscopyForm(forms.Form):
    IMAGE_EXTS = (".tif", ".tiff", ".jpg", ".jpeg", ".png")
    images = MultipleFileField(
        widget=MultipleFileInput(
            attrs={
                "accept": ".tif,.tiff,.jpg,.jpeg,.png",
                "style": INPUT,
                "multiple": True,
            }
        )
    )
    od = forms.FloatField(
        min_value=0,
        widget=forms.NumberInput(
            attrs={"style": INPUT, "step": "any", "placeholder": "0.11"}
        ),
    )
    micro_date = forms.DateField(
        widget=forms.DateInput(attrs={"style": INPUT, "type": "date"})
    )

    def clean_images(self):
        files = self.cleaned_data["images"]
        for f in files:
            if not f.name.lower().endswith(self.IMAGE_EXTS):
                raise forms.ValidationError(f"{f.name}: must be a TIF, JPG, or PNG")
        # Cap the combined size: the view reads every image into memory at once.
        if sum(f.size for f in files) > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(
                f"Images total more than the {MAX_UPLOAD_MB} MB limit."
            )
        return files


class StrainCompositionForm(forms.Form):
    raw_reads = forms.FileField(
        widget=forms.ClearableFileInput(
            attrs={"accept": ".fastq,.fq,.gz", "style": INPUT}
        )
    )
    od = forms.FloatField(
        min_value=0,
        widget=forms.NumberInput(
            attrs={"style": INPUT, "step": "any", "placeholder": "0.11"}
        ),
    )
    seq_date = forms.DateField(
        widget=forms.DateInput(attrs={"style": INPUT, "type": "date"})
    )

    def clean_raw_reads(self):
        f = self.cleaned_data["raw_reads"]
        _check_size(f, MAX_READS_MB)
        return f


class ReferenceGenomeForm(forms.Form):
    # One row per Run strain: `name` is the strain (hard-coded via initial and
    # shown as a label), the user just attaches a genome file for each.
    name = forms.CharField(widget=forms.HiddenInput())
    genome_file = forms.FileField(
        widget=forms.ClearableFileInput(
            attrs={"accept": ".fasta,.fa,.fna", "style": INPUT}
        )
    )


ReferenceGenomeFormSet = forms.formset_factory(ReferenceGenomeForm, extra=0)


MODE_CHOICES = [
    ("batch", "Batch"),
    ("fed-batch", "Fed-batch"),
    ("continuous", "Continuous"),
]


class RunForm(forms.ModelForm):
    # strains/comp are entered as "|"-separated text in the manual form,
    # but stored as JSON lists on the model.
    mode = forms.ChoiceField(
        choices=MODE_CHOICES, widget=forms.Select(attrs={"style": INPUT})
    )
    strains = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"style": INPUT, "placeholder": "StrainA|StrainB|StrainC|StrainD"}
        ),
    )
    comp = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"style": INPUT, "placeholder": "30|25|15|30"}),
    )

    class Meta:
        model = Run
        fields = [
            "rca_id",
            "batch_id",
            "name",
            "status",
            "start_date",
            "end_date",
            "oper",
            "reac",
            "vol",
            "media",
            "mode",
            "temp",
            "ph",
            "strains",
            "comp",
        ]
        widgets = {
            "rca_id": forms.NumberInput(
                attrs={"min": "0", "style": INPUT, "placeholder": "31"}
            ),
            "batch_id": forms.NumberInput(
                attrs={"min": "0", "style": INPUT, "placeholder": "3"}
            ),
            "name": forms.TextInput(
                attrs={
                    "style": INPUT,
                    "placeholder": "RCA31B3 - Baseline Media",
                }
            ),
            "status": forms.Select(attrs={"style": INPUT}),
            "start_date": forms.DateInput(attrs={"style": INPUT, "type": "date"}),
            "end_date": forms.DateInput(attrs={"style": INPUT, "type": "date"}),
            "oper": forms.TextInput(
                attrs={"style": INPUT, "placeholder": "J. Smith"}
            ),
            "reac": forms.TextInput(attrs={"style": INPUT, "placeholder": "R7"}),
            "vol": forms.NumberInput(
                attrs={"style": INPUT, "step": "any", "placeholder": "5"}
            ),
            "media": forms.TextInput(attrs={"style": INPUT, "placeholder": "P0%"}),
            "temp": forms.NumberInput(
                attrs={"min": "0", "style": INPUT, "step": "any", "placeholder": "45"}
            ),
            "ph": forms.NumberInput(
                attrs={"min": "0", "style": INPUT, "step": "any", "placeholder": "7.0"}
            ),
        }

    def clean_strains(self):
        raw = self.cleaned_data["strains"].strip()
        if not raw:
            return []
        strains = []
        bad = []
        for part in raw.split("|"):
            s = part.strip()
            if not s:
                continue
            strains.append(s)
            if not STRAIN_NAME_RE.match(s):
                bad.append(s)
        if bad:
            raise forms.ValidationError(
                "Strain names may only contain letters, numbers, spaces, "
                f". + or -. Invalid: {', '.join(bad)}"
            )
        return strains

    def clean_comp(self):
        raw = self.cleaned_data["comp"].strip()
        if not raw:
            return []
        try:
            return [float(c) for c in raw.split("|") if c.strip()]
        except ValueError:
            raise forms.ValidationError(
                "Composition must be | separated numbers, e.g. 52|48"
            )

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        end_date = cleaned.get("end_date")
        # A finished run needs an end date; a running one must not have one yet.
        if status == "complete" and not end_date:
            self.add_error("end_date", "A complete run needs an end date.")
        if status == "running" and end_date:
            self.add_error("end_date", "A running run can't have an end date yet.")

        # slug (rca_id-batch_id) is unique; check here so a duplicate is a clean
        # form error instead of an IntegrityError 500. all_objects includes
        # soft-deleted runs, which still occupy the slug.
        rca_id = cleaned.get("rca_id")
        batch_id = cleaned.get("batch_id")
        if rca_id and batch_id:
            slug = f"{rca_id}-{batch_id}"
            clash = Run.all_objects.filter(slug=slug).exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError(
                    f"A run with RCA {rca_id} / batch {batch_id} already exists."
                )
        return cleaned
