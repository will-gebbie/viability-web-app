"""HTTP wrapper around the strain-composition aligner.

POST /strain_comp — multipart form:
  reads:          the Nanopore raw reads (FASTQ), one file
  <strain name>:  one reference genome FASTA per strain (field name IS the strain)
Query: name = sample id. Returns {"sample": id, "abundance": {strain: fraction}}.

Single-sample synchronous version of the old align.smk pipeline: cat the refs,
minimap2 (keeping secondary hits for the EM pass), then em_abundance.
"""

import hmac
import os
import subprocess
import tempfile

from em_abundance import abundance, load_paf
from flask import Flask, jsonify, request

app = Flask(__name__)

# Required, with no fallback: if this service is deployed to a public endpoint, an
# unauthenticated request would leak results and burn compute. Failing to boot is
# the safe default. /health is behind it too, so the service exposes nothing
# without the token. (The __main__ selftest below needs it set:
# `SERVICE_TOKEN=x python server.py`.)
TOKEN = os.environ["SERVICE_TOKEN"]


@app.before_request
def require_token():
    sent = request.headers.get("X-Service-Token", "")
    if not hmac.compare_digest(sent, TOKEN):
        return jsonify(error="unauthorized"), 401

PENALTY = 0.25
# minimap2 threads. Defaults to one fewer than the host's cores: everything shares
# one small VPS, and an alignment at full width starves the portal's gunicorn
# workers, which reads to users as "the site froze" for the whole run.
# ponytail: cores-1 is a heuristic, not a scheduler — set STRAIN_THREADS explicitly
# if the container is CPU-limited (os.cpu_count() cannot see a cgroup quota) or if
# strain ever gets a box to itself.
THREADS = os.environ.get("STRAIN_THREADS") or str(max(1, (os.cpu_count() or 2) - 1))


@app.get("/health")
def health():
    return "ok"


VALID_BASES = set("ATGCRYSWKMBDHVNX")


def tag(strain, contig):
    """Strain-qualified contig id, so identical contig names across refs stay distinct."""
    return f"{'_'.join(strain.split())}_{contig}"


def parse_refs(ref_paths):
    """{contig: strain} and {strain: genome_size}, straight from the reference FASTAs.

    Raises ValueError on anything that isn't nucleotide FASTA.
    """
    contig2strain = {}
    genome_size = {}
    for strain, path in ref_paths.items():
        size = 0
        with open(path, errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                seq = line.strip()
                if lineno == 1 and not seq.startswith(">"):
                    raise ValueError(
                        f"{strain}: not a FASTA file — the first line must start with '>'"
                    )
                if seq.startswith(">"):
                    contig = seq[1:].split()
                    if not contig:
                        raise ValueError(f"{strain}: line {lineno} has an empty header")
                    contig2strain[tag(strain, contig[0])] = strain
                    continue
                bad = set(seq.upper()) - VALID_BASES
                if bad:
                    raise ValueError(
                        f"{strain}: line {lineno} has invalid sequence "
                        f"characters: {''.join(sorted(bad))}"
                    )
                size += len(seq)
        if not size:
            raise ValueError(f"{strain}: FASTA contains no sequence")
        genome_size[strain] = size
    return contig2strain, genome_size


@app.post("/strain_comp")
def strain_comp():
    sample = request.args.get("name", "sample")
    with tempfile.TemporaryDirectory() as d:
        reads_path = os.path.join(d, "reads.fastq")
        request.files["reads"].save(reads_path)

        # One reference FASTA per strain; the form field name is the strain.
        # Concatenate into a single target so a read's near-equal hits to every
        # high-ANI strain survive minimap2's secondary-alignment output.
        ref_paths = {}
        combined = os.path.join(d, "combined_ref.fasta")
        with open(combined, "w") as out:
            for strain, f in request.files.items():
                if strain == "reads":
                    continue
                # basename: the field name is the strain, and a "/" or ".." in it
                # would otherwise write outside the tempdir. The portal validates
                # strain names, but this service is its own trust boundary.
                p = os.path.join(d, f"{os.path.basename(strain)}.fna")
                f.save(p)
                ref_paths[strain] = p
                # Rewrite ">contig_id ..." as ">strain_contig_id ..." to match
                # the keys parse_refs builds — see tag().
                with open(p, errors="replace") as fh:
                    for line in fh:
                        if line.startswith(">"):
                            out.write(f">{tag(strain, line[1:].lstrip())}")
                        else:
                            out.write(line)

        try:
            contig2strain, genome_size = parse_refs(ref_paths)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        paf = os.path.join(d, "aligned.paf")
        with open(paf, "w") as pafout:
            subprocess.run(
                [
                    "minimap2",
                    "-t",
                    THREADS,
                    "-x",
                    "map-ont",
                    "-N",
                    "10",
                    "-p",
                    "0.8",
                    combined,
                    reads_path,
                ],
                stdout=pafout,
                check=True,
            )

        read_hits, read_block = load_paf(paf, contig2strain)
        abund = abundance(read_hits, read_block, genome_size, PENALTY)

    return jsonify({"sample": sample, "abundance": abund})


if __name__ == "__main__":
    # python server.py — smoke test for the FASTA validation.
    def _write(d, text):
        p = os.path.join(d, "t.fna")
        with open(p, "w") as fh:
            fh.write(text)
        return {"s1": p}

    def _fails(d, text):
        try:
            parse_refs(_write(d, text))
        except ValueError:
            return True
        return False

    with tempfile.TemporaryDirectory() as d:
        c2s, sizes = parse_refs(_write(d, ">c1 desc\nACGTN\n\nrykm\n"))
        assert c2s == {"s1_c1": "s1"}, c2s
        assert sizes == {"s1": 9}, sizes
        # Same contig name in two refs must not collide.
        assert tag("A", "contig_1") != tag("B", "contig_1")
        assert tag("Strain A", "c1") == "Strain_A_c1"
        assert _fails(d, "ACGT\n"), "missing header not caught"
        assert _fails(d, ">c1\nACGTZ\n"), "bad base not caught"
        assert _fails(d, ">c1\n"), "empty sequence not caught"
        assert _fails(d, ">\nACGT\n"), "empty header not caught"
    print("ok")
