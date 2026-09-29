import csv
import io
import json
import math
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from itertools import zip_longest

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_not_required, permission_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .forms import (
    MAX_CSV_MB,
    FlowCytometryForm,
    MicroscopyForm,
    ReferenceGenomeFormSet,
    RunForm,
    StrainCompositionForm,
)
from .models import FlowResult, MicroResult, Run, SeqResult

OD_DT_FORMAT = "%m/%d/%Y %H:%M"  # CSV datetime column, e.g. 05/02/2026 09:42


def _parse_od_csv(text):
    """CSV columns Datetime,OD,CFU -> (iso_datetimes, od_values, cfu_values).

    CFU column is optional; missing/blank CFU cells become None. Skips header/bad rows.
    """
    times, ods, cfus = [], [], []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            dt = datetime.strptime(row[0].strip(), OD_DT_FORMAT)
            od = float(row[1])
        except ValueError:
            continue  # header row or malformed line
        if len(row) > 2 and row[2].strip():
            try:
                cfu = float(row[2])
            except ValueError:
                cfu = None
        else:
            cfu = None

        times.append(dt.isoformat())
        ods.append(od)
        cfus.append(cfu)

    return _sort_by_time(times, ods, cfus)


def _first_negative(ods, cfus):
    """Error message for the first negative OD/CFU value, or None. Blank CFUs skipped."""
    for od in ods:
        if od < 0:
            return f"OD {od} is negative."
    for cfu in cfus:
        if cfu is not None and cfu < 0:
            return f"CFU {cfu} is negative."
    return None


def _first_out_of_bounds(iso_times, start, end):
    """First timepoint string outside [start, end], or None. Unparseable ones skipped."""
    for t in iso_times:
        try:
            d = datetime.fromisoformat(t).date()
        except ValueError:
            continue
        if start and d < start:
            return t
        if end and d > end:
            return t
    return None


def _sort_by_time(times, ods, cfus):
    """Order the three parallel series by timepoint.

    key= is load-bearing: without it, two rows sharing a time *and* an OD fall
    through to comparing CFUs, and a blank CFU (None) against a number is a
    TypeError.
    """
    rows = sorted(zip(times, ods, cfus), key=lambda row: row[0])
    if not rows:
        return [], [], []
    s_times, s_ods, s_cfus = zip(*rows)
    return list(s_times), list(s_ods), list(s_cfus)


def _pk(value):
    """A pk arriving from POST/GET: None unless it's digits.

    filter(pk="oops") raises ValueError, i.e. a 500 rather than a no-op.
    """
    if value and value.isdigit():
        return value
    return None


def _read_csv(uploaded):
    """(text, None) or (None, error). Guards size before .read() loads it all.

    The form-based uploads get their ceiling from forms.py; these two CSV
    endpoints don't go through a FileField, so the cap lives here.
    """
    if uploaded.size > MAX_CSV_MB * 1024 * 1024:
        return None, f"File is larger than the {MAX_CSV_MB} MB limit for a CSV."
    try:
        return uploaded.read().decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "File is not valid UTF-8 text. Upload a CSV."


def _hx_redirect(name, *args):
    resp = HttpResponse(status=204)
    resp["HX-Redirect"] = reverse(name, args=args)
    return resp


def _date_error(run, results, field, value, replace_id, label):
    """Shared date validation for the flow/micro/seq uploads, or None if OK.

    Rejects a future date, one before the run started, or a second result on a
    date that already has one (unless we're replacing that very result).
    """
    if value > timezone.localdate():
        return f"{label} date can't be in the future."
    if run.start_date and value < run.start_date:
        return f"{label} date can't be before the run's start date."
    clash = results.filter(**{field: value}).exclude(pk=replace_id or None)
    if clash.exists():
        return (
            f"A {label.lower()} result already exists for this date. Edit it instead."
        )
    return None


def _od_error(times, ods, cfus, run):
    """Shared OD/CFU series validation (future / before-start / negative), or None."""
    future = _first_out_of_bounds(times, None, timezone.localdate())
    if future:
        return f"Timepoint {future} is in the future."
    bad = _first_out_of_bounds(times, run.start_date, None)
    if bad:
        return f"Timepoint {bad} is before the run's start date."
    return _first_negative(ods, cfus)


def _viab_txt(viab):
    if viab is None:
        return "None"
    return f"{math.floor(viab)}%"


def _od_txt(od):
    if od is None:
        return "None"
    return f"{od}"


def _flow_txt(flow, viab, od):
    # Viable cells/mL = flow density (cells/µL → cells/mL) × 10 × OD, scaled by
    # viability.
    if flow is None or viab is None or od is None:
        return "None"
    return f"{flow.cells_per_uL * 1000 * 10 * od * (math.floor(viab) / 100):.2e}"


def _chart_data(run):
    # Derived from the owning models — micro/strain series are cycled in the UI,
    # OD/CFU series from Run's JSON fields.
    # Flow results are keyed by day so a micro result can pick up the flow run
    # from the same date (if any) when the UI cycles through timepoints.
    flow_by_day = {}
    for f in run.flow_results.order_by("flow_date"):
        if f.flow_date:
            flow_by_day[f.flow_date] = f
    micro_series = []
    for m in run.micro_results.order_by("micro_date"):
        if m.micro_date:
            label = m.micro_date.strftime("%b %-d, %Y")
        else:
            label = m.created.strftime("%b %-d, %Y")
        if m.micro_date:
            flow = flow_by_day.get(m.micro_date)
        else:
            flow = None
        micro_series.append(
            {
                "label": label,
                "labels": ["Live", "Dying", "Dormant", "Dead"],
                "data": [m.live, m.dying, m.dormant, m.dead],
                "err": [
                    m.live_std or 0,
                    m.dying_std or 0,
                    m.dormant_std or 0,
                    m.dead_std or 0,
                ],
                "n": m.num_imgs,
                "viab_txt": _viab_txt(m.viab),
                "flow_txt": _flow_txt(flow, m.viab, m.od),
                "flow_od": _od_txt(m.od),
            }
        )
    # Strain composition is cycled in the UI: "Initial" from the Run itself,
    # then one entry per sequencing result (oldest first).
    strain_series = []
    if run.comp:
        strain_series.append(
            {"label": "Initial", "labels": run.strains, "data": run.comp}
        )
    for s in run.seq_results.order_by("created"):
        if not s.comp:
            continue
        if s.seq_date:
            label = s.seq_date.strftime("%b %-d, %Y")
        else:
            label = s.created.strftime("%b %-d, %Y")
        strain_series.append({"label": label, "labels": s.strains, "data": s.comp})
    return {
        "times": run.od_time,  # ISO datetime strings, shared x-axis for OD + CFU
        "od": run.od_data,
        "cfu": run.cfu_data,  # aligned to times; may contain nulls
        "strain_series": strain_series,
        "micro_series": micro_series,
    }


FILTERS = ["all", "running", "complete"]

# Reusable role gates (raise 403 for a logged-in user missing the perm).
can_add = permission_required("portal.add_run", raise_exception=True)  # import
can_edit = permission_required("portal.change_run", raise_exception=True)  # upload/edit
can_hard_delete = permission_required("portal.delete_run", raise_exception=True)


@login_not_required
@never_cache
def signin(request):
    if request.user.is_authenticated:
        return redirect("portal:runs")

    # AuthenticationForm reads "username"/"password" from POST, runs
    # authenticate() (via our EmailBackend), and reports bad-credential errors.
    # ?next= comes through as a GET param on the redirect and as a hidden field
    # on submit; check both.
    nxt = request.POST.get("next") or request.GET.get("next", "")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())  # starts the session cookie
        # Only follow `next` if it's a local URL — blocks open-redirect attacks.
        safe = url_has_allowed_host_and_scheme(
            nxt,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        if safe:
            return redirect(nxt)
        return redirect("portal:runs")

    return render(request, "portal/signin.html", {"form": form, "next": nxt})


@require_POST
def signout(request):
    # POST-only: a GET logout can be fired by a link or <img> another site
    # embeds, letting them sign a user out unbidden (a minor CSRF).
    logout(request)
    return redirect("portal:signin")


def runs(request: HttpRequest):
    active = request.GET.get("filter", "all")
    # prefetch: the grid reads r.latest_viab per card, which sorts micro_results
    # in Python — so one query feeds every card instead of one query each.
    runs = Run.objects.prefetch_related("micro_results")
    if active in ("running", "complete"):
        runs = runs.filter(status=active)
    ctx = {
        "runs": runs,
        "filters": FILTERS,
        "active": active,
        # Counted over every live run, not the filtered page — otherwise
        # ?filter=complete reports "0 currently running".
        "running": Run.objects.filter(status="running").count(),
    }
    if request.headers.get("HX-Request"):
        return render(request, "portal/_run_grid.html", ctx)
    return render(request, "portal/runs.html", ctx)


def overview(request, run_id):
    r = get_object_or_404(Run, slug=run_id)
    chart_data = _chart_data(r)
    # Tiles start on the most recent timepoint; the UI swaps them as micro is cycled.
    if chart_data["micro_series"]:
        latest = chart_data["micro_series"][-1]
        viab_txt, flow_txt, flow_od = (
            latest["viab_txt"],
            latest["flow_txt"],
            latest["flow_od"],
        )
    else:
        viab_txt, flow_txt, flow_od = "None", "None", ""
    return render(
        request,
        "portal/overview.html",
        {
            "run": r,
            "chart_data": chart_data,
            "viab_txt": viab_txt,
            "last_od": r.last_od,
            "last_cfu": r.last_cfu,
            "flow_txt": flow_txt,
            "flow_od": flow_od,
        },
    )


@can_edit
@require_POST
def delete_result(request, run_id, kind):
    # kind ("micro"/"flow"/"seq") comes from the urlconf, not the request.
    # POST-only: a delete on GET slips past CSRF (GET is a "safe" method Django
    # doesn't token-check)
    run = get_object_or_404(Run, slug=run_id)
    delete_id = _pk(request.POST.get("delete") or request.GET.get("delete"))
    if delete_id:
        getattr(run, f"{kind}_results").filter(pk=delete_id).delete()
    return redirect("portal:overview", run_id=run_id)


@can_edit
def delete_run(request, run_id):
    # Soft delete: flag the row so it drops out of the normal list but survives.
    if request.method == "POST":
        run = get_object_or_404(Run, slug=run_id)
        run.soft_delete()
    return redirect("portal:runs")


@can_hard_delete
def trash(request):
    deleted = Run.all_objects.filter(deleted_at__isnull=False)
    return render(request, "portal/trash.html", {"runs": deleted})


@can_edit
def restore_run(request, run_id):
    if request.method == "POST":
        run = get_object_or_404(Run.all_objects, slug=run_id)
        run.restore()
    return redirect("portal:trash")


@can_hard_delete
def hard_delete_run(request, run_id):
    # Permanent: actually removes the row (and cascades to its results).
    if request.method == "POST":
        Run.all_objects.filter(slug=run_id).delete()
    return redirect("portal:trash")


@can_edit
def edit_run(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    # GET: tile chooser (Run / Microscopy / Flow Cytometry).
    return render(request, "portal/edit_run.html", {"run": run})


@can_edit
def run_partial(request, run_id, template):
    # Render-only htmx partials (edit/od tile choosers). `template` is fixed in
    # the urlconf, never from the request.
    run = get_object_or_404(Run, slug=run_id)
    return render(request, template, {"run": run})


@can_edit
def edit_run_form(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    if request.method == "POST":
        form = RunForm(request.POST, instance=run)
        if form.is_valid():
            run = form.save()  # slug is set in Run.save()
            return _hx_redirect("portal:overview", run.slug)
    else:
        # strains/comp are CharField overrides; pre-fill from the stored JSON lists.
        form = RunForm(
            instance=run,
            initial={
                "strains": "|".join(run.strains),
                "comp": "|".join(str(c) for c in run.comp),
            },
        )
    return render(request, "portal/_edit_run_form.html", {"run": run, "form": form})


# The upload endpoint each result kind's "add new" path redirects to. seq is the
# odd one out — its uploader is upload-strain, not upload-seq.
_UPLOAD_URL = {
    "micro": "portal:upload-micro",
    "flow": "portal:upload-flow",
    "seq": "portal:upload-strain",
}


@can_edit
def edit_results(request, run_id, kind):
    # kind ("micro"/"flow"/"seq") comes from the urlconf. Pick an existing result
    # to edit, or — with none yet — go straight to that kind's upload flow.
    run = get_object_or_404(Run, slug=run_id)
    results = getattr(run, f"{kind}_results").order_by("-created")
    if not results:
        return _hx_redirect(_UPLOAD_URL[kind], run.slug)
    return render(
        request, f"portal/_edit_{kind}_choice.html", {"run": run, "results": results}
    )


@can_edit
def edit_od(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    # Pre-populate the manual-entry rows with every stored timepoint.
    # zip_longest guards against legacy series of differing lengths.
    rows = list(zip_longest(run.od_time, run.od_data, run.cfu_data))
    return render(request, "portal/_edit_od_form.html", {"run": run, "rows": rows})


@can_edit
def upload_od(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    error = None
    if request.method == "POST" and request.FILES.get("csv"):
        text, error = _read_csv(request.FILES["csv"])
        if not error:
            times, ods, cfus = _parse_od_csv(text)
            if not times:
                # Refuse to overwrite existing data with a file we couldn't parse.
                error = "No valid rows found. Expected columns: Datetime, OD, CFU."
            else:
                error = _od_error(times, ods, cfus, run)
            if not error:
                run.od_time, run.od_data, run.cfu_data = times, ods, cfus
                run.save(update_fields=["od_time", "od_data", "cfu_data"])
                return redirect("portal:overview", run_id=run_id)
    # GET: dual-tile chooser page (upload a template CSV, or enter manually).
    return render(request, "portal/upload_od.html", {"run": run, "error": error})


@can_edit
def upload_od_manual(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    if request.method == "POST":
        times, ods, cfus = [], [], []
        rows = zip(
            request.POST.getlist("datetime"),
            request.POST.getlist("od"),
            request.POST.getlist("cfu"),
        )
        for dt, od, cfu in rows:
            if not dt.strip() or not od.strip():
                continue  # need at least a timepoint + OD
            try:
                # Normalised to the isoformat() the CSV path stores, so a manual
                # point and an imported one at the same minute compare equal —
                # and so an unparseable timepoint never reaches od_time.
                dt_val = datetime.fromisoformat(dt.strip()).isoformat()
                od_val = float(od)
            except ValueError:
                continue
            if cfu.strip():
                try:
                    cfu_val = float(cfu)
                except ValueError:
                    cfu_val = None
            else:
                cfu_val = None
            times.append(dt_val)
            ods.append(od_val)
            cfus.append(cfu_val)
        s_times, s_ods, s_cfus = _sort_by_time(times, ods, cfus)
        hx = request.headers.get("HX-Request")
        error = _od_error(s_times, s_ods, s_cfus, run)
        if error:
            # Edit form posts via htmx: re-render it in place with the rows the
            # user typed. The upload-flow form is a plain POST -> full page.
            if hx:
                rows = list(
                    zip_longest(
                        request.POST.getlist("datetime"),
                        request.POST.getlist("od"),
                        request.POST.getlist("cfu"),
                    )
                )
                return render(
                    request,
                    "portal/_edit_od_form.html",
                    {"run": run, "rows": rows, "error": error},
                )
            return render(
                request, "portal/upload_od.html", {"run": run, "error": error}
            )
        run.od_time, run.od_data, run.cfu_data = s_times, s_ods, s_cfus
        run.save(update_fields=["od_time", "od_data", "cfu_data"])
        if hx:
            return _hx_redirect("portal:overview", run_id)
    return redirect("portal:overview", run_id=run_id)


def _write_multipart(body, boundary, parts):
    """Write a multipart/form-data body into `body`, returning its length in bytes.

    parts: (field_name, filename, open file). Files are copied in chunks and never
    held in memory — a Nanopore read set runs to gigabytes, and buffering one
    OOM-kills the gunicorn worker (taking its other threads' requests with it).
    Leaves `body` rewound and ready to send.
    """
    for field, filename, fh in parts:
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            (
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        fh.seek(0)
        shutil.copyfileobj(fh, body)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    length = body.tell()
    body.seek(0)
    return length


def _service_json(req, timeout):
    """Call an analysis service, raising its own error text rather than "HTTP Error 400".

    Callers catch OSError, not URLError: urllib only wraps a timeout while *sending*
    the request, so waiting out `timeout` on the response — the long part of an
    analysis — surfaces a bare TimeoutError. Both are OSError subclasses.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # Every service rejects bad input with {"error": ...}; show that message.
        body = e.read().decode(errors="replace")[:500]
        try:
            detail = json.loads(body)["error"]
        except (ValueError, KeyError, TypeError):
            detail = body.strip() or e.reason
        raise ValueError(detail) from None


def _count_fcs(fcs_bytes, od, volume, name):
    """POST raw .fcs bytes to the counter service, return its result dict."""
    query = urllib.parse.urlencode({"od": od, "uL": volume, "name": name})
    req = urllib.request.Request(
        f"{settings.COUNTER_URL}?{query}",
        data=fcs_bytes,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "X-Service-Token": settings.SERVICE_TOKEN,
        },
    )
    return _service_json(req, settings.COUNTER_TIMEOUT)


@can_edit
def upload_flow(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    error = None
    # Set when replacing an existing result (from the edit intermediate step).
    replace_id = _pk(request.POST.get("replace") or request.GET.get("replace"))
    if request.method == "POST":
        form = FlowCytometryForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["fcs_file"]
            fd = form.cleaned_data["flow_date"]
            error = _date_error(
                run, run.flow_results, "flow_date", fd, replace_id, "Flow"
            )
            if not error:
                try:
                    result = _count_fcs(
                        f.read(),
                        form.cleaned_data["od"],
                        form.cleaned_data["volume"],
                        f.name,
                    )
                    FlowResult.objects.create(
                        run=run,
                        fcs_name=f.name,
                        flow_date=fd,
                        volume_uL=form.cleaned_data["volume"],
                        od=form.cleaned_data["od"],
                        n_cells=result["n_cells"],
                        cells_per_uL=result["cells_per_uL"],
                        cells_per_uL_per_OD=result.get("cells_per_uL_per_OD"),
                    )
                except (OSError, KeyError, ValueError) as e:
                    error = f"Counting failed: {e}"
                else:
                    if replace_id:
                        run.flow_results.filter(pk=replace_id).delete()
                    return _hx_redirect("portal:overview", run_id)
    else:
        form = FlowCytometryForm()
    ctx = {"run": run, "form": form, "error": error, "replace_id": replace_id}
    if request.headers.get("HX-Request"):
        return render(request, "portal/_flow_form.html", ctx)
    return render(request, "portal/upload_flow.html", ctx)


def _post_strain(reads, genomes, sample):
    """POST reads + one reference FASTA per strain to the aligner, return its result.

    reads: an uploaded file. genomes: list of (strain, uploaded file) — the strain
    becomes the multipart field name so the service maps each genome to it.
    """
    boundary = "----strain-boundary-labportal"
    parts = [("reads", reads.name, reads)]
    for strain, gf in genomes:
        parts.append((strain, gf.name, gf))

    query = urllib.parse.urlencode({"name": sample})
    with tempfile.TemporaryFile() as body:
        length = _write_multipart(body, boundary, parts)
        req = urllib.request.Request(
            f"{settings.STRAIN_URL}?{query}",
            data=body,  # a handle, not bytes — urllib streams it
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                # Required: http.client can't size a file object, and without this
                # urllib silently falls back to chunked transfer-encoding.
                "Content-Length": str(length),
                "X-Service-Token": settings.SERVICE_TOKEN,
            },
        )
        return _service_json(req, settings.STRAIN_TIMEOUT)


@can_edit
def upload_strain(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    error = None
    replace_id = _pk(request.POST.get("replace") or request.GET.get("replace"))
    # One genome-upload row per strain, name hard-coded from the run.
    initial = []
    for s in run.strains:
        initial.append({"name": s})
    if request.method == "POST":
        form = StrainCompositionForm(request.POST, request.FILES)
        formset = ReferenceGenomeFormSet(request.POST, request.FILES)
        if form.is_valid() and formset.is_valid():
            reads = form.cleaned_data["raw_reads"]
            genomes = []
            for f in formset:
                gf = f.cleaned_data["genome_file"]
                genomes.append((f.cleaned_data["name"], gf))
            sd = form.cleaned_data["seq_date"]
            error = _date_error(
                run, run.seq_results, "seq_date", sd, replace_id, "Sequencing"
            )
            if not error:
                try:
                    result = _post_strain(reads, genomes, run.slug)
                    abund = result["abundance"]
                    # Keep the run's strain order; scale fractions to percentages.
                    strains = []
                    comp = []
                    for s in run.strains:
                        if s in abund:
                            strains.append(s)
                            comp.append(round(abund[s] * 100, 2))
                    SeqResult.objects.create(
                        run=run,
                        strains=strains,
                        comp=comp,
                        od=form.cleaned_data["od"],
                        seq_date=form.cleaned_data["seq_date"],
                    )
                except (OSError, KeyError, ValueError) as e:
                    error = f"Alignment failed: {e}"
                else:
                    if replace_id:
                        run.seq_results.filter(pk=replace_id).delete()
                    return redirect("portal:overview", run_id=run_id)
    else:
        form = StrainCompositionForm()
        formset = ReferenceGenomeFormSet(initial=initial)
    ctx = {
        "run": run,
        "form": form,
        "formset": formset,
        "error": error,
        "replace_id": replace_id,
    }
    return render(request, "portal/upload_strain.html", ctx)


def _post_micro(images, name):
    """POST image files as multipart to the microscopy service, return its result dict.

    images: list of uploaded files.
    """
    boundary = "----micro-boundary-labportal"
    parts = []
    for f in images:
        parts.append(("images", f.name, f))

    query = urllib.parse.urlencode({"name": name})
    with tempfile.TemporaryFile() as body:
        length = _write_multipart(body, boundary, parts)
        req = urllib.request.Request(
            f"{settings.MICRO_URL}?{query}",
            data=body,  # a handle, not bytes — urllib streams it
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length),
                "X-Service-Token": settings.SERVICE_TOKEN,
            },
        )
        return _service_json(req, settings.MICRO_TIMEOUT)


@can_edit
def upload_micro(request, run_id):
    run = get_object_or_404(Run, slug=run_id)
    error = None
    # Set when replacing an existing result (from the edit intermediate step).
    replace_id = _pk(request.POST.get("replace") or request.GET.get("replace"))
    if request.method == "POST":
        form = MicroscopyForm(request.POST, request.FILES)
        if form.is_valid():
            imgs = form.cleaned_data["images"]
            names = ", ".join(f.name for f in imgs)
            md = form.cleaned_data["micro_date"]
            error = _date_error(
                run, run.micro_results, "micro_date", md, replace_id, "Microscopy"
            )
            if not error:
                try:
                    result = _post_micro(imgs, names)
                    MicroResult.objects.create(
                        run=run,
                        micro_name=names[:255],
                        micro_date=md,
                        od=form.cleaned_data["od"],
                        num_imgs=result["num_imgs"],
                        live=result["live"],
                        dormant=result["dormant"],
                        dying=result["dying"],
                        dead=result["dead"],
                        live_std=result["live_std"],
                        dormant_std=result["dormant_std"],
                        dying_std=result["dying_std"],
                        dead_std=result["dead_std"],
                        viab=result["viab"],
                    )
                except (OSError, KeyError, ValueError) as e:
                    error = f"Microscopy analysis failed: {e}"
                else:
                    if replace_id:
                        run.micro_results.filter(pk=replace_id).delete()
                    return _hx_redirect("portal:overview", run_id)

    else:
        form = MicroscopyForm()
    ctx = {"run": run, "form": form, "error": error, "replace_id": replace_id}
    if request.headers.get("HX-Request"):
        return render(request, "portal/_micro_form.html", ctx)
    return render(request, "portal/upload_micro.html", ctx)


@can_add
def import_run(request):
    if request.method == "POST":
        if request.FILES.get("data_file"):
            return _import_run_csv(request)
        form = RunForm(request.POST)
        if form.is_valid():
            form.save()  # slug is set in Run.save()
            return _hx_redirect("portal:runs")
        # Invalid: re-render the manual form (swapped into #import-body) with errors.
        return render(request, "portal/_import_manual.html", {"form": form})
    return render(request, "portal/import_run.html")


def _import_run_csv(request):
    """Parse an uploaded CSV, one Run per row via RunForm. All-or-nothing."""
    text, error = _read_csv(request.FILES["data_file"])
    if error:
        return render(request, "portal/_import_upload.html", {"errors": [error]})
    rows = list(csv.DictReader(io.StringIO(text)))
    forms = []
    for row in rows:
        forms.append(RunForm(row))
    errors = []
    slugs = []
    for i, form in enumerate(forms):
        if not form.is_valid():
            errors.append(f"Row {i + 2}: {form.errors.as_text()}")
        # Slug from cleaned_data, not the raw row: CharField strips whitespace,
        # so " 31" and "31" are the same slug by the time it reaches the DB.
        rca_id = form.cleaned_data.get("rca_id")
        batch_id = form.cleaned_data.get("batch_id")
        if rca_id and batch_id:
            slugs.append(f"{rca_id}-{batch_id}")
    if not rows:
        errors.append("File has no data rows.")
    # Duplicate rca_id-batch_id within the same file (each row's form validates
    # against the DB independently, so it can't see its twin here).
    dups = []
    for slug, count in Counter(slugs).items():
        if count > 1:
            dups.append(slug)
    if dups:
        errors.append(f"Duplicate rows in file: {', '.join(sorted(dups))}")
    if errors:
        return render(request, "portal/_import_upload.html", {"errors": errors})
    # All-or-nothing: a failure mid-loop rolls back every row.
    with transaction.atomic():
        for form in forms:
            form.save()  # slug is set in Run.save()
    return _hx_redirect("portal:runs")


@can_add
def import_partial(request, template):
    # Render-only import tile choosers; `template` fixed in the urlconf.
    return render(request, template)


@can_add
def import_manual(request):
    return render(request, "portal/_import_manual.html", {"form": RunForm()})
