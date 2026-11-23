"""Copernicus Data Space Ecosystem (CDSE) downloader — PS-10 Sentinel-2 pairs.

Uses ``eodag`` (https://eodag.readthedocs.io) as the provider client so we get
OAuth2 / STAC search, resumable downloads and automatic token refresh for free.
This is the recommended way to access CDSE: hand-rolled scripts usually target
the *old* ``search.dataspace.copernicus.eu`` OData service, which is dead.

Providers (``--provider``):
  cop_dataspace      CDSE STAC API, OAuth2 client-credentials (default)
  cop_dataspace_s3   CDSE via S3 protocol — most robust for long/bulk runs;
                     generate S3 keys at
                     https://eodata-s3keysmanager.dataspace.copernicus.eu/panel/s3-credentials
  creodias / wekeo_main / earth_search   mirrors, if CDSE misbehaves

Credentials (eodag reads them from the environment or the user config at
``~/.config/eodag/eodag.yml``; this repo keeps them in ``.envrc.local``,
see ``.envrc.local.example``).
Env var names are lower-cased ``EODAG__<provider>__auth__credentials__<key>``.

No API key/secret pair is needed for the default OAuth path: CDSE uses a fixed
*public* client (``cdse-public``) with the OAuth2 password grant, so the two
slots are simply your account e-mail + password.

  cop_dataspace (default — your normal CDSE account email + password):
      EODAG__cop_dataspace__auth__credentials__username  (= your email)
      EODAG__cop_dataspace__auth__credentials__password  (= your password)
  cop_dataspace_s3 (optional, for long/bulk runs — S3 key pair from
      https://eodata-s3keysmanager.dataspace.copernicus.eu/panel/s3-credentials):
      EODAG__cop_dataspace_s3__auth__credentials__aws_access_key_id
      EODAG__cop_dataspace_s3__auth__credentials__aws_secret_access_key

Only the free open Sentinel-2 L2A products are used (10 m, the alternate
sensor in the PS-10 statement); data is free under the Copernicus licence.

Examples (from the repo root):
    # search Sentinel-2 L2A over the Delhi-NCR urban benchmark site
    python scripts/download_copernicus.py \\
        --site delhi-urban --start 2025-01-01 --end 2025-02-28 --preview

    # same search, then download up to 2 granules
    python scripts/download_copernicus.py \\
        --site delhi-urban --start 2025-01-01 --end 2025-02-28 \\
        --download --max-downloads 2 \\
        --out data/copernicus
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path


def log(msg: str) -> None:
    print(f"[copernicus] {msg}", flush=True)


# The 6 terrains named in the PS-10 problem statement (BENCHMARK_DATA.md §1).
BENCHMARK_SITES: dict[str, tuple[float, float]] = {
    "himalaya-snow": (74.3909, 34.0531),
    "thar-desert": (70.9543, 26.9027),
    "delhi-urban": (77.6126, 28.1740),
    "forest": (84.2129, 23.7380),
    "hill": (76.7904, 31.2834),
    "plain": (77.6157, 13.3143),
}

DEFAULT_COLLECTION = "S2_MSI_L2A"  # Sentinel-2 L2A, 10 m — the PS-10 alt sensor


def polygon_bbox(lon: float, lat: float, radius_km: float) -> dict[str, float]:
    """Return an eodag ``geom`` dict (bbox) around a centre, mirroring the
    Bhoonidhi script's circular AOI (bbox is the STAC-friendly equivalent)."""
    dlat = math.degrees(radius_km / 6371.0)
    dlon = math.degrees(radius_km / 6371.0) / math.cos(math.radians(lat))
    return {
        "lonmin": lon - dlon,
        "latmin": lat - dlat,
        "lonmax": lon + dlon,
        "latmax": lat + dlat,
    }


def geom_from_aoi_file(path: str) -> dict[str, float]:
    geo = json.loads(Path(path).read_text())
    from shapely import geometry  # local import; only needed for --aoi-file

    shape = geometry.shape(geo.get("geometry", geo))
    minx, miny, maxx, maxy = shape.bounds
    return {"lonmin": minx, "latmin": miny, "lonmax": maxx, "latmax": maxy}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    default_out = Path(__file__).resolve().parents[1] / "data" / "copernicus"
    ap = argparse.ArgumentParser(
        description="Search & download CDSE Sentinel-2 L2A products for PS-10 "
        "via eodag (mirrors the Bhoonidhi downloader CLI).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "--site",
        choices=sorted(BENCHMARK_SITES),
        help="PS-10 benchmark site (§6.i terrains)",
    )
    ap.add_argument("--point", help="AOI centre as 'lon,lat'")
    ap.add_argument("--radius-km", type=float, default=25.0)
    ap.add_argument("--aoi-file", help="GeoJSON file whose bounds the search")
    ap.add_argument("--bbox", help="'west,south,east,north'")
    ap.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    ap.add_argument("--end", help="end date YYYY-MM-DD (default: today)")
    ap.add_argument(
        "--collections",
        nargs="+",
        default=[DEFAULT_COLLECTION],
        help="eodag collection id(s) (see `eodag search --help`)",
    )
    ap.add_argument(
        "--provider",
        default="cop_dataspace",
        help="eodag provider (cop_dataspace | cop_dataspace_s3 | "
        "creodias | wekeo_main | earth_search, ...)",
    )
    ap.add_argument("--limit", type=int, default=10, help="max granules to return")
    ap.add_argument("--cloud-max", type=float, default=80.0, help="max cloud/shadow %%")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--preview", action="store_true", help="search + list only (no download)"
    )
    group.add_argument(
        "--download", action="store_true", help="search, then download matched granules"
    )
    ap.add_argument(
        "--max-downloads", type=int, default=2, help="cap on granules fetched"
    )
    ap.add_argument("--out", type=Path, default=default_out, help="output directory")
    return ap.parse_args(argv)


def search(args: argparse.Namespace):
    try:
        from eodag import EODataAccessGateway
        from eodag.utils.exceptions import EodagError
    except ImportError as e:  # pragma: no cover
        raise SystemExit("eodag is not installed — run `pixi install` first.") from e

    has_aoi = args.site or args.point or args.aoi_file or args.bbox
    if not has_aoi:
        raise SystemExit("one of --site / --point / --aoi-file / --bbox is required")

    if args.aoi_file:
        geom = geom_from_aoi_file(args.aoi_file)
    elif args.bbox:
        west, south, east, north = (float(v) for v in args.bbox.split(","))
        geom = {"lonmin": west, "latmin": south, "lonmax": east, "latmax": north}
    else:
        if args.site:
            lon, lat = BENCHMARK_SITES[args.site]
            log(
                f"benchmark site '{args.site}': lon={lon} lat={lat} r={args.radius_km} km"
            )
        else:
            lon, lat = (float(v) for v in args.point.split(","))
        geom = polygon_bbox(lon, lat, args.radius_km)
    log(
        f"geom: {geom} | {args.start} → {args.end or 'today'} | "
        f"cloud<{args.cloud_max:.0f}% | provider={args.provider}"
    )

    # cop_dataspace (OData) consumes the cloud cap via the ``eo:cloud_cover``
    # query key with a ``<=`` bound; the older ``cloudCover_lt`` spelling is
    # silently ignored by that provider.
    dag = EODataAccessGateway()
    try:
        hits = dag.search(
            collection=args.collections[0],
            provider=args.provider,
            start=args.start,
            end=args.end,
            geom=geom,
            max_records=args.limit,
            **{"eo:cloud_cover": args.cloud_max},
        )
    except EodagError as e:
        # every meaningful eodag failure (auth, no data, unknown provider/collection,
        # network) subclasses EodagError — catch that base rather than bare Exception.
        msg = str(e)
        if "auth" in msg.lower() or "credential" in msg.lower():
            msg += (
                "\nHint: the eodag provider needs credentials. For cop_dataspace "
                "that is just your CDSE account email + password (no API key/secret "
                "pair), e.g. put in .envrc.local:\n"
                "  EODAG__cop_dataspace__auth__credentials__username=<your email>\n"
                "  EODAG__cop_dataspace__auth__credentials__password=<your password>\n"
                "For cop_dataspace_s3 use its S3 key pair (aws_access_key_id / "
                "aws_secret_access_key) from the CDSE S3 key manager."
            )
        raise SystemExit(f"search on {args.provider} failed: {e}\n{msg}")
    return dag, hits


def _hprop(hit, *keys) -> str | None:
    """First non-None of ``hit.properties[k]`` across candidate STAC/legacy keys."""
    props = getattr(hit, "properties", None) or {}
    for k in keys:
        if props.get(k) not in (None, ""):
            return props[k]
    return None


def summarize(hits) -> None:
    if not len(hits):
        log(
            "no granules matched the search — try a wider date range, site, or "
            "--cloud-max 100"
        )
        return
    dates = sorted(
        (d or "")[:10]
        for d in (_hprop(h, "datetime", "start_datetime") for h in hits)
        if d
    )
    log(
        f"summary: {len(hits)} granule(s)"
        + (f" | dates {dates[0]} … {dates[-1]}" if dates else "")
    )
    for h in hits:
        cid = _hprop(h, "id", "s2:tile_id", "uid")
        ptype = _hprop(h, "product:type", "productType", "processing:level") or ""
        inst = _hprop(h, "instruments") or ""
        cloud = None
        for k in ("cloudCover", "eo:cloud_cover"):
            if (v := (getattr(h, "properties", None) or {}).get(k)) is not None:
                cloud = v
                break
        cloud_s = f" | cloud={cloud:.0f}%" if cloud is not None else ""
        log(f"  {cid}\n    {ptype} | {inst}{cloud_s}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    dag, hits = search(args)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    hits_path = out_dir / "search_results.geojson"
    hits_path.write_text(json.dumps(hits.as_dict(), indent=2))
    log(f"results written to {hits_path}")
    summarize(hits)

    if args.preview:
        return

    pick = hits[: args.max_downloads] if args.max_downloads > 0 else hits
    if not pick:
        return
    log(
        f"downloading {len(pick)} granule(s) from {args.provider} "
        f"(each L2A granule ≈ 200–600 MiB zip)"
    )
    t0 = time.time()
    paths = dag.download_all(
        pick, wait=0.5, output_dir=str(out_dir), extract=True, delete_archive=False
    )

    manifest = {
        "downloaded": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "results": {"ok": len(paths), "elapsed_s": round(time.time() - t0, 1)},
        "paths": [str(p) for p in paths],
        "items": [
            {
                "id": _hprop(h, "id", "uid"),
                "productType": _hprop(h, "product:type", "productType"),
            }
            for h in pick
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ok = sum(1 for p in paths if p and Path(p).exists())
    log(f"done: {ok}/{len(pick)} granule(s) in {out_dir} ({round(time.time() - t0)} s)")
    missing = [
        str(_hprop(h, "id", "uid"))
        for h, p in zip(pick, paths)
        if not (p and Path(p).exists())
    ]
    if missing:
        log("missing/failed: " + "; ".join(missing))


if __name__ == "__main__":
    main()
