# Copernicus Data Space (CDSE) Sentinel-2 Downloader

Search & download **Sentinel-2 L2A** granules (free, open, 10 m) from the
Copernicus Data Space Ecosystem via [eodag](https://eodag.readthedocs.io) —
OAuth2/STAC search, resumable downloads, automatic token refresh.

This is the currently supported way to access CDSE: hand-rolled scripts
usually target the *old* `search.dataspace.copernicus.eu` OData service,
which is decommissioned.

## Providers (`--provider`)

| provider         | notes                                                        |
|------------------|--------------------------------------------------------------|
| `cop_dataspace`  | CDSE STAC API, OAuth2 password grant (default)               |
| `cop_dataspace_s3` | CDSE via S3 — most robust for long/bulk runs               |
| `creodias`, `wekeo_main`, `earth_search` | mirrors, if CDSE misbehaves |

## Credentials

**No API key / secret pair is needed** for the default OAuth path: CDSE uses a
fixed *public* OAuth2 client (`cdse-public`) with the password grant, so you
just supply your normal **CDSE account email + password**
(from <https://dataspace.copernicus.eu> login):

```bash
export EODAG__cop_dataspace__auth__credentials__username=<your email>
export EODAG__cop_dataspace__auth__credentials__password=<your password>
```

With direnv: copy `.envrc.local.example` to `.envrc.local`, fill it in,
`direnv allow`.

Optional, for long/bulk runs — the `cop_dataspace_s3` provider with an S3 key
pair from
<https://eodata-s3keysmanager.dataspace.copernicus.eu/panel/s3-credentials>:

```bash
export EODAG__cop_dataspace_s3__auth__credentials__aws_access_key_id=...
export EODAG__cop_dataspace_s3__auth__credentials__aws_secret_access_key=...
python scripts/download_copernicus.py --provider cop_dataspace_s3 ...
```

## Setup

This is a pixi project (deps: `eodag`, `shapely`). Credentials stay in
`.envrc.local` (see above).

## Usage

Run from the repo root:

```bash
# search + list Sentinel-2 L2A over the Delhi-NCR benchmark site
pixi run python scripts/download_copernicus.py \
    --site delhi-urban --start 2025-01-01 --end 2025-02-28 --preview

# same search, then download up to 2 granules (each ~200-600 MiB zip, auto-extracted)
pixi run python scripts/download_copernicus.py \
    --site delhi-urban --start 2025-01-01 --end 2025-02-28 \
    --download --max-downloads 2
```

`pixi run preview` / `pixi run download` are pre-wired shortcuts for the
Delhi-NCR Jan–Feb 2025 site.

### AOI options (one required)

| flag        | meaning                                              |
|-------------|------------------------------------------------------|
| `--site`    | one of the 6 PS-10 benchmark sites (`--help` lists them) |
| `--point`   | AOI centre as `lon,lat`, radius via `--radius-km`   |
| `--aoi-file`| GeoJSON file whose bounds the search                 |
| `--bbox`    | `west,south,east,north`                              |

### Other useful flags

- `--cloud-max` — max cloud/shadow % (default 80)
- `--limit` — max granules to return
- `--max-downloads` — cap on granules fetched
- `--out` — output directory (default `data/copernicus/`)
- `--provider` — eodag provider (see table above)

## Output

Per run, in `--out`:

- `search_results.geojson` — the STAC item list returned by the search
- `manifest.json` — provider, per-granule status / elapsed time, paths
- the extracted granule directory (per-band JP2 + metadata) + the original granule zip
