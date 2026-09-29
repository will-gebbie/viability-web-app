"""Resolve read assignments across high-ANI strains by expectation-maximization.

At ~99% ANI a long read maps near-equally to several genomes, so best-hit
assignment is arbitrary. The signal that separates strains is the *difference*
in mismatches at strain-specific variant sites. We give each (read, strain)
pair a likelihood weight = penalty ** mismatches, then run EM:

    E-step: responsibility r[read,s] proportional to theta[s] * weight[read,s]
    M-step: theta[s] = sum of responsibilities for s, normalised

Random ONT errors are shared across a read's alignments, so they cancel in the
E-step normalisation; only the strain-discriminating mismatches drive the split.
theta is the fraction of *reads* per strain. We then soft-assign each read's
*bases* to strains by those EM responsibilities, divide by genome size and
renormalise for relative abundance (cells map ~ bases / genome_size).
"""

import sys
from collections import defaultdict

import pandas as pd


def run_em(read_hits, strains, penalty, max_iter=200, tol=1e-7):
    """read_hits: {read_id: {strain: fewest_mismatches}}. Returns {strain: read_fraction}."""
    # Per-read, per-strain likelihood weight.
    weights = {}
    for read, hits in read_hits.items():
        w = {}
        for strain, mm in hits.items():
            w[strain] = penalty**mm
        weights[read] = w

    # Seed theta from a hard best-hit assignment (+1 pseudocount avoids zero-lock).
    theta = {}
    for strain in strains:
        theta[strain] = 1.0
    for read, hits in read_hits.items():
        best = min(hits, key=hits.get)
        theta[best] += 1.0
    total = sum(theta.values())
    for strain in strains:
        theta[strain] /= total

    for _ in range(max_iter):
        counts = {}
        for strain in strains:
            counts[strain] = 0.0
        for read, w in weights.items():
            denom = 0.0
            for strain, wt in w.items():
                denom += theta[strain] * wt
            if denom == 0.0:
                continue
            for strain, wt in w.items():
                counts[strain] += theta[strain] * wt / denom
        n = sum(counts.values())
        new = {}
        for strain in strains:
            if n > 0:
                new[strain] = counts[strain] / n
            else:
                new[strain] = theta[strain]
        delta = 0.0
        for strain in strains:
            delta += abs(new[strain] - theta[strain])
        theta = new
        if delta < tol:
            break
    return theta


def load_paf(path, contig2strain):
    """Best (fewest-mismatch) alignment of each read to each strain, plus its block length."""
    # PAF 0-indexed: 0=read, 5=target contig, 9=residue matches, 10=block length
    df = pd.read_csv(
        path,
        sep="\t",
        header=None,
        usecols=[0, 5, 9, 10],
        names=["ReadID", "Contig", "Matches", "Block"],
    )
    df["Strain"] = df["Contig"].map(contig2strain)
    df = df.dropna(subset=["Strain"])
    df["Mismatches"] = df["Block"] - df["Matches"]

    read_hits = defaultdict(dict)
    read_block = defaultdict(
        dict
    )  # aligned bases of the chosen alignment per (read, strain)
    for row in df.itertuples(index=False):
        cur = read_hits[row.ReadID].get(row.Strain)
        if cur is None or row.Mismatches < cur:
            read_hits[row.ReadID][row.Strain] = row.Mismatches
            read_block[row.ReadID][row.Strain] = row.Block
    return read_hits, read_block


def base_totals(read_hits, read_block, theta, penalty):
    """Soft-assign each read's aligned bases to strains by final EM responsibility."""
    bases = defaultdict(float)
    for read, hits in read_hits.items():
        w = {}
        denom = 0.0
        for strain, mm in hits.items():
            wt = theta[strain] * penalty**mm
            w[strain] = wt
            denom += wt
        if denom == 0.0:
            continue
        for strain, wt in w.items():
            bases[strain] += read_block[read][strain] * wt / denom
    return bases


def abundance(read_hits, read_block, genome_size, penalty):
    strains = sorted(genome_size)
    theta = run_em(read_hits, strains, penalty)
    bases = base_totals(read_hits, read_block, theta, penalty)
    cov = {}
    for strain in strains:
        cov[strain] = bases[strain] / genome_size[strain]
    tot = sum(cov.values())
    out = {}
    for strain in strains:
        if tot > 0:
            out[strain] = cov[strain] / tot
        else:
            out[strain] = 0.0
    return out


def main(paf, contig2strain, genome_size, penalty, sample, out_path):
    read_hits, read_block = load_paf(paf, contig2strain)
    abund = abundance(read_hits, read_block, genome_size, penalty)
    rows = []
    for strain in sorted(genome_size):
        rows.append(
            {"Sample": sample, "Strain": strain, "RelativeAbundance": abund[strain]}
        )
    pd.DataFrame(rows).to_json(out_path, orient="records", indent=2)


def _selfcheck():
    # 70 reads truly from A, 20 from B, 10 from C; true strain has 0 mismatches,
    # the others 5. EM should recover ~0.7 / 0.2 / 0.1 (equal genome sizes).
    read_hits = {}
    i = 0
    for strain, n in (("A", 70), ("B", 20), ("C", 10)):
        for _ in range(n):
            hits = {"A": 5, "B": 5, "C": 5}
            hits[strain] = 0
            read_hits[i] = hits
            i += 1
    theta = run_em(read_hits, ["A", "B", "C"], penalty=0.25)
    assert abs(theta["A"] - 0.7) < 0.02, theta
    assert abs(theta["B"] - 0.2) < 0.02, theta
    assert abs(theta["C"] - 0.1) < 0.02, theta
    print("ok", theta)

    # Equal block lengths + equal genomes → base-weighted abundance == read fractions.
    read_block = {}
    for r in read_hits:
        read_block[r] = {}
        for s in read_hits[r]:
            read_block[r][s] = 1000
    genome_size = {"A": 1.0, "B": 1.0, "C": 1.0}
    abund = abundance(read_hits, read_block, genome_size, penalty=0.25)
    assert abs(abund["A"] - 0.7) < 0.02, abund
    assert abs(abund["B"] - 0.2) < 0.02, abund
    assert abs(abund["C"] - 0.1) < 0.02, abund

    # Double the aligned blocks of C's reads → C's base share grows.
    for r in read_hits:
        if read_hits[r]["C"] == 0:
            read_block[r]["C"] = 2000
    skewed = abundance(read_hits, read_block, genome_size, penalty=0.25)
    assert skewed["C"] > abund["C"], skewed
    print("ok bases", abund, skewed)


if "snakemake" in globals():
    main(
        snakemake.input[0],
        snakemake.params.contig2strain,
        snakemake.params.genome_size,
        snakemake.params.penalty,
        snakemake.wildcards.sample,
        snakemake.output[0],
    )
elif __name__ == "__main__":
    _selfcheck()
