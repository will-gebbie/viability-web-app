# LabPortal

A Django web portal for tracking bioreactor runs and the lab data collected on
them. Users record a run (conditions, strain mix, OD/CFU readings), then upload
raw instrument files. The portal sends each file to an analysis service and
charts the result on the run's page:

| Upload | Input | Analysis service | Result |
| --- | --- | --- | --- |
| **Flow cytometry** | `.fcs` file + sample volume & OD | `counter/`: arcsinh-scaled FSC/SSC, gated to the largest HDBSCAN cluster | cells/µL and cells/µL/OD |
| **Microscopy** | `.tif` / `.png` / `.jpg` images, red + green channels | `microscopy/`: Cellpose segmentation, then per-cell z-score vs. background | % live / dormant / dying / dead, with error bars across images |
| **Strain composition** | Nanopore reads (`.fastq`) + one reference FASTA per strain | `strain_composition/`: minimap2 alignment plus expectation-maximization over ambiguous reads | relative abundance per strain |
| **OD / CFU** | CSV or manual entry | none (stored directly) | growth curve |

## Architecture

```
            browser (HTMX + Alpine + Chart.js)
                         │
                  ┌──────▼──────┐
                  │  web :8000  │  Django + gunicorn + WhiteNoise, SQLite
                  └──┬───┬───┬──┘
      X-Service-Token│   │   │
        ┌────────────┘   │   └──────────────┐
┌───────▼──────┐ ┌───────▼───────┐ ┌────────▼────────┐
│ counter:8001 │ │ strain:8003   │ │ microscopy:8002 │
│ HDBSCAN (CPU)│ │ minimap2 + EM │ │ Cellpose (GPU)  │
└──────────────┘ └───────────────┘ └─────────────────┘
```

- **`portal/`** is the Django app: models, views, templates, and the `invite` command.
- **`config/`** holds the Django settings. Everything is configured through environment variables.
- **`scripts/`** holds the analysis code (`fcs_count.py`, `microscopy_analysis.py`, `em_abundance.py`). Each service's Dockerfile copies in the script it needs.
- **`counter/`, `microscopy/`, `strain_composition/`** each contain a small Flask wrapper, a conda `environment.yml`, and a Dockerfile.
- **`deploy/`** has a systemd unit for running the portal on a bare-metal host.

Each analysis service is stateless. It takes one request and returns JSON, and
it refuses to start without `SERVICE_TOKEN`. Because of that, any of them can
run in a local container or on a separate (e.g. GPU) host; switching is a
single URL setting.

### Strain abundance method

When strains share ~99% average nucleotide identity (ANI), a long read maps
almost equally well to several genomes, so assigning each read to its best hit
is essentially arbitrary. `scripts/em_abundance.py` gives each (read, strain)
pair a likelihood of `penalty ** mismatches` and runs EM to estimate the
fraction of reads from each strain. Sequencing errors affect every alignment of
a read equally, so they cancel out; only mismatches at strain-specific sites
shift the estimate. It then splits each read's aligned bases across strains by
those probabilities and normalizes by genome size to get relative cell
abundance.

## Quick start (Docker)

Requirements: Docker with Compose v2. The microscopy service also needs an
NVIDIA GPU and a trained Cellpose model. You can leave microscopy out (see
below).

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```ini
SECRET_KEY=<any long random string>
SERVICE_TOKEN=<any long random string>
DEBUG=True
CELLPOSE_MODEL=/absolute/path/to/your/cellpose/model
```

Generate the random strings with
`python -c "import secrets; print(secrets.token_urlsafe(50))"`.

Start the portal and all three analysis services:

```bash
docker compose --profile local up --build
```

Or run the portal alone. It works fine without the services; only the uploads
that use them will fail:

```bash
docker compose up --build web
```

To run without a GPU, start only the services you need:

```bash
docker compose --profile local up --build web counter strain
```

On startup the web container applies migrations and collects static files. The
database is stored in the `db` Docker volume.

Open <http://localhost:8000>.

For development, `docker compose --profile local watch` syncs changes in
`portal/` and `config/` into the running container and restarts it.

## Creating accounts

Accounts are invite-only, and users sign in with their email address. Every
account has one of three roles:

| Role | Can |
| --- | --- |
| **Viewer** | view runs |
| **Scientist** | view, create, and edit runs; upload data; move runs to the trash |
| **Admin** | everything above, plus permanently deleting runs |

Invite someone:

```bash
docker compose exec web python manage.py invite jane@example.com --role Scientist --first Jane --last Doe
```

This creates the account without a password and prints a one-time link (valid
for 3 days) where the user sets their own. Re-running it for an existing email
issues a new link. If `EMAIL_HOST` is set, users can also reset forgotten
passwords by email; otherwise the email is printed to the container log.

To use Django's admin at `/admin/`:

```bash
docker compose exec web python manage.py createsuperuser
```

## Using the portal

1. **Create a run.** On **Runs**, click **Import New Run**. Either fill in the form or
   upload a CSV based on
   [`run_template.csv`](portal/static/portal/run_template.csv). A run is
   identified by its RCA ID and batch number (both whole numbers), which also
   form its URL (for example, `/runs/31-3/`).
   - `strains` and `comp` are `|`-separated lists in matching order
     (`StrainA|StrainB` / `52|48`). The composition must add up to 100%.
   - A `complete` run needs an end date; a `running` run must not have one.
2. **Add OD/CFU readings.** Enter them by hand or upload a CSV based on
   [`od_template.csv`](portal/static/portal/od_template.csv)
   (`Datetime,OD,CFU`). Readings must fall between the run's start and end
   dates.
3. **Upload data.** From the run page, upload flow cytometry, microscopy, or
   sequencing data. Each upload waits for its analysis to finish, which can take
   several minutes for sequencing and microscopy. The result is saved against
   the run, and the charts update.
4. **Edit or remove.** Use **Edit** to change a run's details, fix OD points,
   or replace or delete individual results. Deleted runs go to **Trash**, where
   they can be restored or, by an Admin, permanently deleted.

Upload limits: 500 MB of images per microscopy batch, 3 GB of reads per
sequencing upload, and 10 MB per CSV.

## Configuration

All settings are environment variables, read from `.env` by Compose. See
[`.env.example`](.env.example).

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | — (required) | Django secret key |
| `SERVICE_TOKEN` | — (required) | Shared secret sent to the analysis services |
| `DEBUG` | `False` under Compose | `True` for local dev. When `False`, HTTPS redirects, secure cookies, and HSTS are enabled. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names |
| `CSRF_TRUSTED_ORIGINS` | — | Comma-separated `https://` origins (production only) |
| `SITE_URL` | `http://localhost:8000` | Base URL used in invite links |
| `COUNTER_URL` / `STRAIN_URL` / `MICRO_URL` | local containers | Point these at remote deployments of the services |
| `CELLPOSE_MODEL` | — | Host path to the Cellpose model, mounted read-only into the microscopy container |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | console backend | SMTP settings for invite and password-reset emails |
| `COUNTER_THREADS` / `STRAIN_THREADS` | CPU cores − 1 | Worker threads for clustering and alignment |

### Timeouts

Each upload has two timeouts: how long the portal waits for the service, and
how long the service's own gunicorn allows a request to run. Each service's
timeout should be longer than the portal's wait for it, so a slow analysis
returns the service's error message instead of a dropped connection. The
portal's own gunicorn timeout should be longer than all of them.

| Service | Portal waits (`*_TIMEOUT`) | Service gunicorn (`*_SERVER_TIMEOUT`) |
| --- | --- | --- |
| counter | 180 s | 300 s |
| strain | 600 s | 900 s |
| microscopy | 600 s | 900 s |
| portal gunicorn (`WEB_SERVER_TIMEOUT`) | | 660 s |

## Production deployment

`deploy/web-app.service` is a systemd unit that runs the portal with gunicorn
from a virtualenv in `/srv/web-app`, bound to `127.0.0.1:8000`. It expects a
reverse proxy or tunnel (for example, Cloudflare Tunnel) in front to terminate
TLS and set `X-Forwarded-Proto`. It runs `migrate` and `collectstatic` on every
start. Set `DEBUG=False`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and
`SITE_URL` in `/srv/web-app/.env`.

## Local development without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY=dev DEBUG=True
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Run the tests:

```bash
SECRET_KEY=test python manage.py test portal
```

The analysis scripts include self-checks. For example,
`python scripts/em_abundance.py` checks that the EM step recovers known strain
mixes, and `SERVICE_TOKEN=x PYTHONPATH=scripts python strain_composition/server.py`
tests the FASTA validation. Both need `pandas`, from
`strain_composition/environment.yml`.
