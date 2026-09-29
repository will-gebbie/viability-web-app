#!/usr/bin/env python

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
from cellpose import models
from cellpose.io import imread

STATUSES = ["live", "dead", "dying", "dormant"]
COLORS = {
    "live": "#2ecc71",
    "dead": "#e74c3c",
    "dying": "#f1c40f",
    "dormant": "#95a5a6",
}


# ── Microscopy viability classification ──────────────────────────────────────
def get_viability_stats(masks, imgs, z_thresh=3.0):
    # Classifies each cell by viability using z-scores relative to background,
    # which naturally compensates for noisy red/green backgrounds.
    # live=green only, dead=red only, dying=red+green, dormant=neither
    per_image = []
    skipped = 0

    for mask, img in zip(masks, imgs):
        if img.ndim < 3 or img.shape[2] < 2:
            skipped += 1
            continue

        red_ch = img[:, :, 0].astype(np.float32)
        green_ch = img[:, :, 1].astype(np.float32)

        bg = mask == 0
        if bg.any():
            bg_r_mean = red_ch[bg].mean()
            bg_r_std = red_ch[bg].std() + 1e-6
            bg_g_mean = green_ch[bg].mean()
            bg_g_std = green_ch[bg].std() + 1e-6
        else:
            bg_r_mean = 0.0
            bg_r_std = 1.0
            bg_g_mean = 0.0
            bg_g_std = 1.0

        cells = {}
        for cid in np.unique(mask):
            if cid == 0:
                continue
            px = mask == cid
            r_z = (red_ch[px].mean() - bg_r_mean) / bg_r_std
            g_z = (green_ch[px].mean() - bg_g_mean) / bg_g_std

            is_red = r_z > z_thresh
            is_green = g_z > z_thresh

            if is_red and is_green:
                status = "dying"
            elif is_green:
                status = "live"
            elif is_red:
                status = "dead"
            else:
                status = "dormant"

            cells[int(cid)] = {
                "status": status,
                "red_z": round(float(r_z), 2),
                "green_z": round(float(g_z), 2),
            }

        counts = {}
        for s in STATUSES:
            counts[s] = 0
        for v in cells.values():
            counts[v["status"]] += 1
        per_image.append({"cells": cells, "counts": counts})

    # Every image was grayscale/single-channel — without red and green there is
    # nothing to classify, and reporting 0% live would look like a real result.
    if not per_image:
        raise ValueError(
            f"no 2-channel fluorescence images — all {skipped} image(s) are "
            "grayscale or single-channel"
        )

    totals = {}
    for s in STATUSES:
        totals[s] = 0
    for img_result in per_image:
        for k in totals:
            totals[k] += img_result["counts"][k]

    return {
        "per_image": per_image,
        "totals": totals,
        "total_cells": sum(totals.values()),
    }


def eval_images(sample_image_dict: dict, model):
    viability_stats = {}
    for sample_id, img_list in sample_image_dict.items():
        imgs = []
        for imfile in img_list:
            imgs.append(imread(imfile))
        masks, _, _ = model.eval(imgs, flow_threshold=0.4, cellprob_threshold=0)
        viability_stats[sample_id] = get_viability_stats(masks, imgs)

    return viability_stats


def analyze(image_paths, model, sample_id="sample"):
    # HTTP-friendly entry: image file list -> viability-percentage dict.
    stats = eval_images({sample_id: image_paths}, model)[sample_id]
    totals = stats["totals"]
    n = stats["total_cells"]
    if not n:
        raise ValueError(f"no cells detected in {len(image_paths)} image(s)")
    pct = {}
    for s in STATUSES:
        pct[s] = round(100 * totals[s] / n, 2)

    # Per-status std-dev across images (error bars). Each image's status % is
    # relative to that image's own cell count.
    per_img_pct = {}
    for s in STATUSES:
        per_img_pct[s] = []
    for img in stats["per_image"]:
        img_n = sum(img["counts"].values())
        if not img_n:
            continue
        for s in STATUSES:
            per_img_pct[s].append(100 * img["counts"][s] / img_n)
    std = {}
    for s in STATUSES:
        if len(per_img_pct[s]) > 1:
            std[s] = round(float(np.std(per_img_pct[s])), 2)
        else:
            std[s] = 0.0

    return {
        "num_imgs": len(image_paths),
        "total_cells": stats["total_cells"],
        "live": pct["live"],
        "dormant": pct["dormant"],
        "dying": pct["dying"],
        "dead": pct["dead"],
        "live_std": std["live"],
        "dormant_std": std["dormant"],
        "dying_std": std["dying"],
        "dead_std": std["dead"],
        "viab": round(pct["live"] + pct["dormant"], 2),
    }


def demo():
    """Self-check: unusable input raises instead of reporting a fake 0% viability."""
    gray = np.zeros((4, 4), dtype=np.uint8)
    mask = np.zeros((4, 4), dtype=int)
    try:
        get_viability_stats([mask], [gray])
    except ValueError:
        pass
    else:
        raise AssertionError("grayscale-only images should raise")

    # One bright-green cell on a dark background → live, and it is counted.
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    img[0, 0, 1] = 255
    mask[0, 0] = 1
    stats = get_viability_stats([mask], [img])
    assert stats["total_cells"] == 1, stats
    assert stats["totals"]["live"] == 1, stats
    print("ok")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("images", nargs="+", help="Image files for one sample")
    p.add_argument("--model", required=True, help="Cellpose model file")
    p.add_argument(
        "--sample-id", dest="sample_id", default="sample", help="Id for this sample"
    )
    p.add_argument("--out", required=True, help="Write microscopy result JSON here")
    args = p.parse_args()

    for img in args.images:
        if not os.path.isfile(img):
            p.error(f"Image not found: {img}")
    if not os.path.exists(args.model):
        p.error(f"Model file not found: {args.model}")

    model = models.CellposeModel(pretrained_model=args.model, gpu=True)
    row = analyze(args.images, model, args.sample_id)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(row, f)

    print(f"microscopy → {args.out}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    else:
        main()
