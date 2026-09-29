from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.signin, name="signin"),
    path("signout/", views.signout, name="signout"),
    path("runs/", views.runs, name="runs"),
    path("trash/", views.trash, name="trash"),
    path("runs/<slug:run_id>/", views.overview, name="overview"),
    path("runs/<slug:run_id>/delete", views.delete_run, name="delete-run"),
    path("runs/<slug:run_id>/restore", views.restore_run, name="restore-run"),
    path("runs/<slug:run_id>/hard-delete", views.hard_delete_run, name="hard-delete-run"),
    path(
        "runs/<slug:run_id>/delete-micro",
        views.delete_result,
        {"kind": "micro"},
        name="delete-micro",
    ),
    path(
        "runs/<slug:run_id>/delete-flow",
        views.delete_result,
        {"kind": "flow"},
        name="delete-flow",
    ),
    path(
        "runs/<slug:run_id>/delete-seq",
        views.delete_result,
        {"kind": "seq"},
        name="delete-seq",
    ),
    path("runs/<slug:run_id>/upload-od", views.upload_od, name="upload-od"),
    path(
        "runs/<slug:run_id>/od-choice",
        views.run_partial,
        {"template": "portal/_od_choice.html"},
        name="od-choice",
    ),
    path(
        "runs/<slug:run_id>/od-upload",
        views.run_partial,
        {"template": "portal/_od_upload.html"},
        name="od-upload",
    ),
    path(
        "runs/<slug:run_id>/od-manual",
        views.run_partial,
        {"template": "portal/_od_manual.html"},
        name="od-manual",
    ),
    path(
        "runs/<slug:run_id>/upload-od-manual",
        views.upload_od_manual,
        name="upload-od-manual",
    ),
    path("runs/<slug:run_id>/upload-flow", views.upload_flow, name="upload-flow"),
    path("runs/<slug:run_id>/upload-strain", views.upload_strain, name="upload-strain"),
    path("runs/<slug:run_id>/upload-micro", views.upload_micro, name="upload-micro"),
    path("runs/<slug:run_id>/edit", views.edit_run, name="edit"),
    path(
        "runs/<slug:run_id>/edit/choice",
        views.run_partial,
        {"template": "portal/_edit_choice.html"},
        name="edit-choice",
    ),
    path("runs/<slug:run_id>/edit/run", views.edit_run_form, name="edit-run-form"),
    path(
        "runs/<slug:run_id>/edit/micro",
        views.edit_results,
        {"kind": "micro"},
        name="edit-micro",
    ),
    path(
        "runs/<slug:run_id>/edit/flow",
        views.edit_results,
        {"kind": "flow"},
        name="edit-flow",
    ),
    path("runs/<slug:run_id>/edit/od", views.edit_od, name="edit-od"),
    path(
        "runs/<slug:run_id>/edit/seq",
        views.edit_results,
        {"kind": "seq"},
        name="edit-seq",
    ),
    path("runs/import", views.import_run, name="import-run"),
    path(
        "runs/import/choice",
        views.import_partial,
        {"template": "portal/_import_choice.html"},
        name="import-choice",
    ),
    path(
        "runs/import/upload",
        views.import_partial,
        {"template": "portal/_import_upload.html"},
        name="import-upload",
    ),
    path("runs/import/manual", views.import_manual, name="import-manual"),
]
