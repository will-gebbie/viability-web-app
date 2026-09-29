import io
import pathlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from unittest import mock

from django.core.exceptions import ValidationError

from .admin import UniqueEmailUserForm
from .forms import MAX_CSV_MB, MAX_READS_MB, RunForm, _check_size
from .models import Run
from django.core.files.uploadedfile import SimpleUploadedFile

from .views import (
    _chart_data,
    _count_fcs,
    _parse_od_csv,
    _post_micro,
    _post_strain,
    _read_csv,
)


class ParseOdCsvTest(SimpleTestCase):
    def test_parses_and_skips_header(self):
        csv = "Datetime,OD\n05/02/2026 09:42,2.1\n05/04/2026 09:42,3.8\n"
        times, ods, cfus = _parse_od_csv(csv)
        self.assertEqual(times, ["2026-05-02T09:42:00", "2026-05-04T09:42:00"])
        self.assertEqual(ods, [2.1, 3.8])
        self.assertEqual(cfus, [None, None])

    def test_skips_malformed_rows(self):
        csv = "05/02/2026 09:42,2.1\nbad,row\n,\n05/06/2026 12:00,5.6\n"
        times, ods, cfus = _parse_od_csv(csv)
        self.assertEqual(times, ["2026-05-02T09:42:00", "2026-05-06T12:00:00"])
        self.assertEqual(ods, [2.1, 5.6])
        self.assertEqual(cfus, [None, None])

    def test_parses_optional_cfu_column(self):
        csv = "05/02/2026 09:42,2.1,1000000\n05/04/2026 09:42,3.8\n"
        times, ods, cfus = _parse_od_csv(csv)
        self.assertEqual(ods, [2.1, 3.8])
        self.assertEqual(cfus, [1000000.0, None])

    def test_rows_tied_on_time_and_od_with_a_blank_cfu(self):
        # Sorting used to fall through to the CFU column and compare None to a
        # float, i.e. TypeError on a duplicated timepoint.
        csv = "05/02/2026 09:42,2.1\n05/02/2026 09:42,2.1,1000\n"
        times, ods, cfus = _parse_od_csv(csv)
        self.assertEqual(times, ["2026-05-02T09:42:00"] * 2)
        self.assertEqual(sorted(c is None for c in cfus), [False, True])


class ReadCsvTest(SimpleTestCase):
    """The two CSV endpoints .read() the file whole; _read_csv caps it first."""

    class _Stub:
        def __init__(self, data):
            self.name = "x.csv"
            self._data = data
            self.size = len(data)

        def read(self):
            return self._data

    def test_oversize_csv_rejected_before_read(self):
        big = self._Stub(b"x" * (MAX_CSV_MB * 1024 * 1024 + 1))
        text, error = _read_csv(big)
        self.assertIsNone(text)
        self.assertIn("MB limit", error)

    def test_small_csv_decodes(self):
        text, error = _read_csv(self._Stub(b"Datetime,OD\n05/02/2026 09:42,2.1\n"))
        self.assertIsNone(error)
        self.assertIn("OD", text)

    def test_non_utf8_rejected(self):
        text, error = _read_csv(self._Stub(b"\xff\xfe not utf8"))
        self.assertIsNone(text)
        self.assertIn("UTF-8", error)


class DeletePermissionTest(TestCase):
    """Groups/permissions come from migrations 0002 + 0004, applied by TestCase."""

    def _user(self, group_name):
        user = User.objects.create_user(username=group_name, password="x")
        user.groups.add(Group.objects.get(name=group_name))
        return user

    def setUp(self):
        self.run = Run.objects.create(slug="rca31-b3", rca_id="31", batch_id="3")

    def test_viewer_cannot_soft_delete(self):
        self.client.force_login(self._user("Viewer"))
        resp = self.client.post(f"/runs/{self.run.slug}/delete")
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(Run.all_objects.get(slug=self.run.slug).deleted_at)

    def test_scientist_soft_deletes_but_cannot_hard_delete(self):
        self.client.force_login(self._user("Scientist"))
        self.client.post(f"/runs/{self.run.slug}/delete")
        # Soft-deleted: hidden from default manager, still present in all_objects.
        self.assertFalse(Run.objects.filter(slug=self.run.slug).exists())
        self.assertIsNotNone(Run.all_objects.get(slug=self.run.slug).deleted_at)
        resp = self.client.post(f"/runs/{self.run.slug}/hard-delete")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Run.all_objects.filter(slug=self.run.slug).exists())

    def test_admin_hard_deletes(self):
        self.client.force_login(self._user("Admin"))
        self.client.post(f"/runs/{self.run.slug}/hard-delete")
        self.assertFalse(Run.all_objects.filter(slug=self.run.slug).exists())

    def test_viewer_cannot_import(self):
        self.client.force_login(self._user("Viewer"))
        self.assertEqual(self.client.get("/runs/import").status_code, 403)

    def test_scientist_can_import(self):
        self.client.force_login(self._user("Scientist"))
        self.assertEqual(self.client.get("/runs/import").status_code, 200)

    def test_running_count_ignores_the_active_filter(self):
        Run.objects.create(slug="1-1", rca_id="1", batch_id="1", status="running")
        user = User.objects.create_user(username="v", password="x")
        user.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(user)
        resp = self.client.get("/runs/?filter=complete")
        self.assertContains(resp, "2 currently running")  # setUp's run + this one

    def test_non_numeric_delete_id_is_a_no_op_not_a_500(self):
        user = User.objects.create_user(username="sci2", password="x")
        user.groups.add(Group.objects.get(name="Scientist"))
        self.client.force_login(user)
        self.run.flow_results.create(volume_uL=1, od=1, n_cells=1, cells_per_uL=1)
        resp = self.client.post(f"/runs/{self.run.slug}/delete-flow?delete=oops")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.run.flow_results.count(), 1)

    def test_runs_and_overview_render(self):
        # Exercises the shared _topbar_right.html include on both pages.
        self.client.force_login(self._user("Scientist"))
        self.assertEqual(self.client.get("/runs/").status_code, 200)
        self.assertEqual(self.client.get(f"/runs/{self.run.slug}/").status_code, 200)

    def test_signout_is_post_only(self):
        user = self._user("Viewer")
        self.client.force_login(user)
        # A GET (link/<img>) must not end the session...
        self.assertEqual(self.client.get("/signout/").status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)
        # ...but the topbar's POST form does.
        self.assertEqual(self.client.post("/signout/").status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class ConsolidatedViewsTest(TestCase):
    """The C refactor: slug derived in Run.save(), and one view each for the
    edit-choice / render-only partials."""

    def setUp(self):
        self.run = Run.objects.create(rca_id="31", batch_id="3")
        user = User.objects.create_user(username="sci", password="x")
        user.groups.add(Group.objects.get(name="Scientist"))
        self.client.force_login(user)

    def test_save_derives_slug_from_ids(self):
        # Even a bogus explicit slug is overwritten with rca_id-batch_id.
        r = Run.objects.create(slug="whatever", rca_id="9", batch_id="1")
        self.assertEqual(r.slug, "9-1")
        r.rca_id = "9"
        r.batch_id = "2"
        r.save()
        self.assertEqual(Run.objects.get(pk=r.pk).slug, "9-2")

    def test_edit_results_redirects_to_upload_when_empty(self):
        resp = self.client.get(f"/runs/{self.run.slug}/edit/seq")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(
            resp["HX-Redirect"], f"/runs/{self.run.slug}/upload-strain"
        )

    def test_edit_results_lists_existing(self):
        self.run.flow_results.create(volume_uL=1, od=1, n_cells=1, cells_per_uL=1)
        resp = self.client.get(f"/runs/{self.run.slug}/edit/flow")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Flow cytometry measurements")

    def test_run_partial_renders_chooser(self):
        resp = self.client.get(f"/runs/{self.run.slug}/od-choice")
        self.assertEqual(resp.status_code, 200)


class RunFormValidationTest(TestCase):
    BASE = {"rca_id": "31", "batch_id": "3", "mode": "batch"}

    def _form(self, **extra):
        return RunForm({**self.BASE, **extra})

    def test_complete_requires_end_date(self):
        self.assertFalse(self._form(status="complete").is_valid())
        self.assertTrue(self._form(status="complete", end_date="2026-05-10").is_valid())

    def test_running_forbids_end_date(self):
        self.assertTrue(self._form(status="running").is_valid())
        self.assertFalse(self._form(status="running", end_date="2026-05-10").is_valid())

    def test_non_numeric_ids_rejected(self):
        # A letter or space here would produce a slug <slug:run_id> can't match.
        for rca, batch in [("abc", "3"), ("31", "x y")]:
            form = RunForm(
                {
                    "rca_id": rca,
                    "batch_id": batch,
                    "name": "n",
                    "status": "running",
                    "mode": "batch",
                }
            )
            self.assertFalse(form.is_valid(), f"{rca}-{batch} should be rejected")

    def test_duplicate_slug_rejected(self):
        Run.objects.create(slug="31-3", rca_id="31", batch_id="3")
        self.assertFalse(self._form(status="running").is_valid())

    def test_soft_deleted_slug_still_blocks(self):
        run = Run.objects.create(slug="31-3", rca_id="31", batch_id="3")
        run.soft_delete()
        self.assertFalse(self._form(status="running").is_valid())

    def test_strain_names_reject_path_and_header_injection(self):
        # These names become filenames and multipart field names on the aligner.
        for evil in ["../../etc/passwd", 'a"b', "a\r\nX-Injected: 1", "a/b"]:
            form = self._form(status="running", strains=evil)
            self.assertFalse(form.is_valid(), evil)
            self.assertIn("strains", form.errors)

    def test_ordinary_strain_names_still_pass(self):
        form = self._form(status="running", strains="StrainA|StrainB|StrainC|StrainD")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["strains"], ["StrainA", "StrainB", "StrainC", "StrainD"]
        )

    def test_composition_must_sum_to_100(self):
        base = {"status": "running", "strains": "A|B"}
        self.assertFalse(self._form(**base, comp="30|40").is_valid())
        self.assertTrue(self._form(**base, comp="60|40").is_valid())
        self.assertTrue(self._form(**base, comp="33.33|33.33|33.34").is_valid())
        self.assertTrue(self._form(status="running").is_valid())  # empty allowed

    def test_end_date_must_be_after_start(self):
        c = {"status": "complete"}
        self.assertFalse(
            self._form(**c, start_date="2026-05-10", end_date="2026-05-10").is_valid()
        )
        self.assertFalse(
            self._form(**c, start_date="2026-05-10", end_date="2026-05-01").is_valid()
        )
        self.assertTrue(
            self._form(**c, start_date="2026-05-01", end_date="2026-05-10").is_valid()
        )

    def test_future_dates_rejected(self):
        from datetime import timedelta

        from django.utils import timezone

        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertFalse(self._form(status="running", start_date=future).is_valid())
        self.assertFalse(
            self._form(
                status="complete", start_date="2026-05-01", end_date=future
            ).is_valid()
        )


class UploadOdTest(TestCase):
    def setUp(self):
        self.run = Run.objects.create(
            slug="31-3",
            rca_id="31",
            batch_id="3",
            od_time=["2026-05-02T09:42:00"],
            od_data=[2.1],
            cfu_data=[None],
        )
        user = User.objects.create_user(username="sci", password="x")
        user.groups.add(Group.objects.get(name="Scientist"))
        self.client.force_login(user)

    def test_unparseable_csv_does_not_wipe_existing_data(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("od.csv", b"nothing,useful\n", content_type="text/csv")
        self.client.post(f"/runs/{self.run.slug}/upload-od", {"csv": bad})
        self.assertEqual(Run.objects.get(slug=self.run.slug).od_data, [2.1])

    def test_od_timepoint_outside_run_dates_rejected(self):
        from datetime import date

        from django.core.files.uploadedfile import SimpleUploadedFile

        self.run.status = "complete"
        self.run.start_date, self.run.end_date = date(2026, 5, 1), date(2026, 5, 31)
        self.run.save()
        # April timepoint precedes start_date -> whole upload rejected, data
        # unchanged. (end_date deliberately doesn't bound OD: readings keep
        # arriving after a run is marked complete.)
        f = SimpleUploadedFile("od.csv", b"Datetime,OD\n04/15/2026 09:42,9.9\n")
        self.client.post(f"/runs/{self.run.slug}/upload-od", {"csv": f})
        self.assertEqual(Run.objects.get(slug=self.run.slug).od_data, [2.1])

    def test_edit_od_out_of_bounds_stays_in_edit_partial(self):
        from datetime import date

        self.run.status = "complete"
        self.run.start_date, self.run.end_date = date(2026, 5, 1), date(2026, 5, 31)
        self.run.save()
        resp = self.client.post(
            f"/runs/{self.run.slug}/upload-od-manual",
            {"datetime": ["2026-04-15T09:42"], "od": ["9.9"], "cfu": [""]},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "before the run")  # edit partial, not upload page
        self.assertContains(resp, "Edit OD / CFU data")
        self.assertEqual(Run.objects.get(slug=self.run.slug).od_data, [2.1])

    def test_negative_od_in_csv_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        f = SimpleUploadedFile("od.csv", b"Datetime,OD\n05/02/2026 09:42,-1.5\n")
        resp = self.client.post(f"/runs/{self.run.slug}/upload-od", {"csv": f})
        self.assertContains(resp, "is negative")
        self.assertEqual(Run.objects.get(slug=self.run.slug).od_data, [2.1])

    def test_manual_entry_normalises_and_drops_unparseable_timepoints(self):
        resp = self.client.post(
            f"/runs/{self.run.slug}/upload-od-manual",
            {
                "datetime": ["2026-05-03T09:42", "not a datetime"],
                "od": ["0.5", "0.9"],
                "cfu": ["", ""],
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        run = Run.objects.get(slug=self.run.slug)
        # Seconds included, matching the CSV path; the junk row never lands.
        self.assertEqual(run.od_time, ["2026-05-03T09:42:00"])
        self.assertEqual(run.od_data, [0.5])

    def test_negative_cfu_manual_rejected(self):
        resp = self.client.post(
            f"/runs/{self.run.slug}/upload-od-manual",
            {"datetime": ["2026-05-02T09:42"], "od": ["0.5"], "cfu": ["-3e8"]},
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(resp, "is negative")
        self.assertEqual(Run.objects.get(slug=self.run.slug).od_data, [2.1])

    def test_flow_date_before_start_rejected(self):
        from datetime import date

        from django.core.files.uploadedfile import SimpleUploadedFile

        self.run.start_date = date(2026, 5, 1)
        self.run.save()
        # Date before start is caught before any network call to the counter service.
        f = SimpleUploadedFile("x.fcs", b"dummy")
        self.client.post(
            f"/runs/{self.run.slug}/upload-flow",
            {"fcs_file": f, "volume": "25", "od": "0.1", "flow_date": "2026-04-01"},
        )
        self.assertFalse(self.run.flow_results.exists())

    def test_flow_date_in_future_rejected(self):
        from datetime import timedelta

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils import timezone

        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        f = SimpleUploadedFile("x.fcs", b"dummy")
        self.client.post(
            f"/runs/{self.run.slug}/upload-flow",
            {"fcs_file": f, "volume": "25", "od": "0.1", "flow_date": future},
        )
        self.assertFalse(self.run.flow_results.exists())

    def test_od_future_timepoint_rejected(self):
        from datetime import timedelta

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils import timezone

        future = timezone.localdate() + timedelta(days=1)
        csv = f"Datetime,OD\n{future:%m/%d/%Y} 09:42,9.9\n".encode()
        self.client.post(
            f"/runs/{self.run.slug}/upload-od",
            {"csv": SimpleUploadedFile("od.csv", csv)},
        )
        self.assertEqual(Run.objects.get(slug=self.run.slug).od_data, [2.1])


class ImportRunViewTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="sci", password="x")
        user.groups.add(Group.objects.get(name="Scientist"))
        self.client.force_login(user)

    BASE = {"rca_id": "31", "batch_id": "3", "mode": "batch"}

    def test_invalid_manual_import_rerenders_partial(self):
        # running + end_date is invalid; must come back as the form partial (200),
        # not a redirect — so htmx swaps it into the styled #import-body.
        resp = self.client.post(
            "/runs/import", {**self.BASE, "status": "running", "end_date": "2026-05-10"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "have an end date yet")  # apostrophe gets escaped
        self.assertFalse(Run.objects.exists())

    def test_valid_manual_import_hx_redirects(self):
        resp = self.client.post("/runs/import", {**self.BASE, "status": "running"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["HX-Redirect"], "/runs/")
        self.assertTrue(Run.objects.filter(slug="31-3").exists())

    def test_csv_duplicate_detected_despite_whitespace(self):
        # " 31" and "31" are the same slug once the form strips them, so the
        # in-file duplicate check has to run on cleaned values.
        csv_text = (
            "rca_id,batch_id,mode,status\n 31 ,3,batch,running\n31,3,batch,running\n"
        )
        f = SimpleUploadedFile("runs.csv", csv_text.encode(), content_type="text/csv")
        resp = self.client.post("/runs/import", {"data_file": f})
        self.assertContains(resp, "Duplicate rows in file: 31-3")
        self.assertFalse(Run.objects.exists())


class InviteTest(TestCase):
    """Accounts are invite-only: no public signup, and the invite link is the
    only way to get a password onto a new account."""

    def _invite(self, email, **kwargs):
        out = io.StringIO()
        call_command("invite", email, stdout=out, **kwargs)
        for line in out.getvalue().splitlines():
            if "/password/set/" in line:
                return urlparse(line.strip()).path  # drop SITE_URL's host
        self.fail(f"no invite link in output:\n{out.getvalue()}")

    def test_no_public_signup_route(self):
        self.assertEqual(self.client.get("/users/new/").status_code, 404)

    def test_duplicate_email_cannot_sign_in_and_admin_blocks_creating_one(self):
        one = User.objects.create_user(username="a", email="a@example.com", password="x")
        self.assertTrue(self.client.login(username="a@example.com", password="x"))
        # auth_user has no unique constraint on email, so a second row can exist —
        # sign-in by email then refuses both rather than guessing which is meant.
        User.objects.create_user(username="b", email="A@example.com", password="x")
        self.client.logout()
        self.assertFalse(self.client.login(username="a@example.com", password="x"))
        # ...and the admin form refuses to create that row in the first place.
        form = UniqueEmailUserForm(
            instance=User.objects.get(username="b"), data={"email": one.email}
        )
        form.is_valid()
        self.assertIn("email", form.errors)

    def test_invite_link_lets_them_set_a_password_and_sign_in(self):
        link = self._invite("newbie@example.com")
        user = User.objects.get(email="newbie@example.com")
        self.assertTrue(user.groups.filter(name="Viewer").exists())
        self.assertFalse(user.has_usable_password())

        # The confirm view redirects to a token-less URL, then takes the POST.
        resp = self.client.get(link)
        self.client.post(
            resp.url, {"new_password1": "sW9!zqx2LmPz", "new_password2": "sW9!zqx2LmPz"}
        )

        self.assertTrue(
            self.client.login(username="newbie@example.com", password="sW9!zqx2LmPz")
        )
        self.assertEqual(self.client.get("/runs/").status_code, 200)

    def test_invite_link_is_single_use(self):
        link = self._invite("once@example.com")
        resp = self.client.get(link)
        self.client.post(
            resp.url, {"new_password1": "sW9!zqx2LmPz", "new_password2": "sW9!zqx2LmPz"}
        )
        # Token hashes the password, so setting one invalidates the link.
        self.assertContains(self.client.get(link), "invalid")

    def test_reset_ignores_accounts_that_never_set_a_password(self):
        # PasswordResetForm skips unusable-password users, so a pending invite
        # can't be hijacked into an email. The invite link is the only way in.
        self._invite("pending@example.com")
        self.client.post("/password/reset/", {"email": "pending@example.com"})
        self.assertEqual(mail.outbox, [])

    def test_forgot_password_flow_renders_portal_templates(self):
        """Walks every reset page + the email, so a broken template fails here."""
        User.objects.create_user(
            username="forgot@example.com",
            email="forgot@example.com",
            password="oLd!pass9Qtz",
        )

        self.assertContains(self.client.get("/password/reset/"), "LabPortal")
        resp = self.client.post(
            "/password/reset/", {"email": "forgot@example.com"}, follow=True
        )
        self.assertContains(resp, "Check your email")

        body = mail.outbox[0].body
        self.assertEqual(mail.outbox[0].subject, "Set your LabPortal password")
        link = None
        for word in body.split():
            if "/password/set/" in word:
                link = urlparse(word).path
        self.assertIsNotNone(link, f"no link in email:\n{body}")

        # The confirm view redirects to a token-less URL before showing the form.
        resp = self.client.get(link, follow=True)
        self.assertContains(resp, "Set your password")
        resp = self.client.post(
            resp.redirect_chain[-1][0],
            {"new_password1": "sW9!zqx2LmPz", "new_password2": "sW9!zqx2LmPz"},
            follow=True,
        )
        self.assertContains(resp, "Password set")

    def test_role_flag_and_reissue_does_not_duplicate(self):
        self._invite("sci@example.com", role="Scientist")
        self._invite("sci@example.com")
        self.assertEqual(User.objects.filter(email="sci@example.com").count(), 1)
        user = User.objects.get(email="sci@example.com")
        self.assertTrue(user.groups.filter(name="Scientist").exists())


class UploadSizeCapTest(SimpleTestCase):
    """Read sets get a 3000 MB ceiling of their own; everything else stays at 500."""

    class _Stub:
        # Real UploadedFiles would mean allocating gigabytes; only .name/.size matter.
        def __init__(self, mb):
            self.name = "reads.fastq.gz"
            self.size = mb * 1024 * 1024

    def test_reads_ceiling(self):
        _check_size(self._Stub(2500), MAX_READS_MB)  # a typical 1-2 GB run passes
        with self.assertRaises(ValidationError):
            _check_size(self._Stub(3001), MAX_READS_MB)

    def test_default_ceiling_unchanged(self):
        _check_size(self._Stub(499))
        with self.assertRaises(ValidationError):
            _check_size(self._Stub(501))


class PostServiceTest(SimpleTestCase):
    """Multipart bodies are streamed from a temp file rather than built in memory —
    a 1-2 GB read set would otherwise OOM the worker. The framing has to survive
    that, and Content-Length has to match or urllib silently goes chunked."""

    def _capture(self):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["headers"] = req.headers
            sent["timeout"] = timeout
            body = req.data
            if hasattr(body, "read"):
                # Read here: the temp file closes when the helper returns. The
                # counter still posts plain bytes — only multipart is streamed.
                body = body.read()
            sent["body"] = body
            return io.BytesIO(b"{}")

        return sent, fake_urlopen

    @override_settings(SERVICE_TOKEN="s3cret", MICRO_TIMEOUT=600)
    def test_micro_sends_token_length_and_framing(self):
        sent, fake_urlopen = self._capture()
        img = SimpleUploadedFile("a.tif", b"PIXELS")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            _post_micro([img], "sample")
        # urllib title-cases header names.
        self.assertEqual(sent["headers"]["X-service-token"], "s3cret")
        self.assertEqual(sent["timeout"], 600)
        self.assertEqual(sent["headers"]["Content-length"], str(len(sent["body"])))
        self.assertIn(b'name="images"; filename="a.tif"', sent["body"])
        self.assertIn(b"PIXELS", sent["body"])

    @override_settings(SERVICE_TOKEN="s3cret", STRAIN_TIMEOUT=600)
    def test_strain_names_each_field_after_its_strain(self):
        sent, fake_urlopen = self._capture()
        reads = SimpleUploadedFile("reads.fastq.gz", b"READS")
        genome = SimpleUploadedFile("s1.fna", b">c1\nACGT\n")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            _post_strain(reads, [("Strain A", genome)], "rca9-b1")
        self.assertEqual(sent["headers"]["X-service-token"], "s3cret")
        self.assertEqual(sent["headers"]["Content-length"], str(len(sent["body"])))
        self.assertIn(b'name="reads"; filename="reads.fastq.gz"', sent["body"])
        self.assertIn(b'name="Strain A"; filename="s1.fna"', sent["body"])
        self.assertIn(b"READS", sent["body"])
        self.assertTrue(sent["body"].endswith(b"--\r\n"))  # closing boundary

    @override_settings(SERVICE_TOKEN="s3cret", COUNTER_TIMEOUT=180)
    def test_counter_sends_token(self):
        sent, fake_urlopen = self._capture()
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            _count_fcs(b"FCSDATA", od=2.0, volume=100, name="s")
        self.assertEqual(sent["headers"]["X-service-token"], "s3cret")
        self.assertEqual(sent["timeout"], 180)


class NoExternalScriptsTest(SimpleTestCase):
    """Every executable byte is served by us.

    A <script src> pointing at a CDN means whoever controls that host can run
    code inside an authenticated session, so the libraries live in
    static/portal/vendor/ instead. Stylesheets (the Geist webfont) are exempt:
    they don't execute.
    """

    def test_no_template_loads_a_remote_script(self):
        templates = pathlib.Path(__file__).parent / "templates"
        offenders = []
        for path in templates.rglob("*.html"):
            for match in re.finditer(r"<script[^>]+src=\"(https?:)?//", path.read_text()):
                offenders.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(offenders, [])


class ServiceTimeoutTest(TestCase):
    """Waiting out the timeout on a *response* raises a bare TimeoutError, not a
    URLError — catching only the latter turned a slow analysis into a 500."""

    def test_flow_upload_reports_a_timeout_instead_of_500(self):
        run = Run.objects.create(slug="rca4-b1", rca_id="4", batch_id="1")
        user = User.objects.create_user(username="sci3", password="x")
        user.groups.add(Group.objects.get(name="Scientist"))
        self.client.force_login(user)

        def timeout(req, timeout=None):
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", timeout):
            resp = self.client.post(
                f"/runs/{run.slug}/upload-flow",
                {
                    "fcs_file": SimpleUploadedFile("a.fcs", b"FCS"),
                    "volume": "25",
                    "od": "0.1",
                    "flow_date": "2026-04-01",
                },
                HTTP_HX_REQUEST="true",
            )
        self.assertContains(resp, "Counting failed")
        self.assertFalse(run.flow_results.exists())


class ChartDataMicroFlowTest(TestCase):
    """Cycling micro timepoints in the UI swaps the viability/flow tiles with it."""

    def test_micro_series_pairs_flow_from_same_day(self):
        run = Run.objects.create(slug="rca9-b1", rca_id="9", batch_id="1")
        day1 = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        run.micro_results.create(micro_date=day1, viab=80.4, od=0.5, num_imgs=3)
        run.micro_results.create(micro_date=day2, viab=50.0, od=0.5, num_imgs=2)
        run.flow_results.create(
            flow_date=day1, volume_uL=100, od=2.0, n_cells=1000, cells_per_uL=10.0
        )
        series = _chart_data(run)["micro_series"]
        self.assertEqual([s["label"] for s in series], ["May 2, 2026", "May 4, 2026"])
        self.assertEqual(series[0]["viab_txt"], "80%")  # floored
        # cells/µL → cells/mL, × 10 × OD, and viab (a percent) as a fraction: × 0.80.
        self.assertEqual(
            series[0]["flow_txt"], f"{10.0 * 1000 * 10 * 0.5 * (80 / 100):.2e}"
        )
        self.assertEqual(series[1]["flow_txt"], "None")  # no flow run that day

    def test_micro_without_od_shows_none_instead_of_crashing(self):
        run = Run.objects.create(slug="rca9-b2", rca_id="9", batch_id="2")
        day = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
        run.micro_results.create(micro_date=day, viab=80.0, num_imgs=3)
        run.flow_results.create(
            flow_date=day, volume_uL=100, od=2.0, n_cells=1000, cells_per_uL=10.0
        )
        series = _chart_data(run)["micro_series"]
        self.assertEqual(series[0]["flow_txt"], "None")


class DeleteResultTest(TestCase):
    """One view backs delete-micro/-flow/-seq; the kind comes from the urlconf."""

    def setUp(self):
        self.run = Run.objects.create(slug="rca7-b2", rca_id="7", batch_id="2")
        user = User.objects.create_user(username="sci", password="x")
        user.groups.add(Group.objects.get(name="Scientist"))
        self.client.force_login(user)

    def test_get_cannot_delete(self):
        # The whole point of the fix: a delete must not fire on a GET (no CSRF
        # check there), so an address-bar paste / <a> / <img> can't destroy data.
        flow = self.run.flow_results.create(
            volume_uL=1, od=1, n_cells=1, cells_per_uL=1
        )
        resp = self.client.get(f"/runs/{self.run.slug}/delete-flow?delete={flow.id}")
        self.assertEqual(resp.status_code, 405)  # method not allowed
        self.assertTrue(self.run.flow_results.filter(pk=flow.id).exists())

    def test_deletes_flow_and_seq(self):
        flow = self.run.flow_results.create(
            volume_uL=100, od=2.0, n_cells=1000, cells_per_uL=10.0
        )
        seq = self.run.seq_results.create(od=2.0, strains=["a"], comp=[100])
        self.client.post(f"/runs/{self.run.slug}/delete-flow?delete={flow.id}")
        self.client.post(f"/runs/{self.run.slug}/delete-seq?delete={seq.id}")
        self.assertEqual(self.run.flow_results.count(), 0)
        self.assertEqual(self.run.seq_results.count(), 0)

    def test_viewer_cannot_delete(self):
        seq = self.run.seq_results.create(od=2.0, strains=["a"], comp=[100])
        viewer = User.objects.create_user(username="view", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        resp = self.client.post(f"/runs/{self.run.slug}/delete-seq?delete={seq.id}")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.run.seq_results.count(), 1)
