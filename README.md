# htsvoice-repo

Static, content-addressed registry and mirror for redistributable HTS voice models.

Production origin: `https://htsvoice-repo.alcnaka.com`

## Design

Git is the source of truth for metadata. Cloudflare R2 stores immutable model/source blobs and generated JSON metadata. The public hostname is attached directly to the R2 bucket as a Custom Domain.

A release is identified by two versions:

- `upstream_version`: the version claimed by the upstream project.
- `revision`: the snapshot observed by this repository. If an upstream silently replaces a file without changing its version, add a new `+rN` release instead of modifying the old lock.

Example: `mei@1.8+r1`, then `mei@1.8+r2` if MMDAgent later changes the 1.8 archive.

Immutable objects are addressed by SHA-256:

```text
/v1/blobs/sha256/ab/<sha256>.htsvoice
/v1/sources/sha256/ab/<sha256>/<upstream-filename>
/v1/licenses/sha256/ab/<sha256>.txt
```

Once published, these keys must never be overwritten or deleted during ordinary deployments.

## Registry endpoints

```text
/v1/index.json
/v1/voices/<voice>.json
/v1/releases/<voice>/<release>.json
```

`index.json` is uploaded last during deployment so clients do not observe a new index before referenced metadata is present.

## Included metadata definitions

Each `voices/<id>/voice.toml` records:

- identity: ID, display name, description
- language: language and locale
- publisher
- upstream homepage, distribution page, original download URL and original archive filename
- upstream version + repository revision
- observed timestamp
- SPDX license ID, canonical license URL, attribution string
- license file path inside the upstream archive
- variants/styles and their original paths
- release lifecycle: `pending`, `current`, `superseded`, `deprecated`, `withdrawn`

`lock.json`, generated from the actual upstream bytes, records:

- source archive SHA-256 + size
- license file SHA-256 + size
- each `.htsvoice` SHA-256 + size
- selected HTSVOICE header metadata (sample rate, frame period, states, streams, etc.)

The lock is deliberately separate from `voice.toml`: descriptive/provenance metadata stays human-editable while content identities are machine-generated.

## First publication of a release

New releases start as `status = "pending"` so CI can validate metadata without pretending the bytes were already verified.

```bash
python tools/registry.py validate
python tools/registry.py lock mei 1.8+r1
```

Inspect and commit `voices/mei/lock.json`, then change the release status to `current` (or another published status). A lock is immutable: if the same upstream URL later returns different bytes, `lock` refuses to overwrite the existing entry. Add `1.8+r2` instead.

Build metadata locally:

```bash
python tools/registry.py build-metadata --out dist
```

Stage a release locally (downloads upstream and verifies it against the committed lock):

```bash
python tools/registry.py stage-release mei 1.8+r1 --out dist
```

## Upstream disappearance and mutation

The deploy job checks R2 before contacting upstream. If all immutable objects for a locked release already exist, upstream is **not fetched at all**. This lets deployments continue even when the original website disappears.

If an object is missing from R2, deployment downloads the upstream archive and verifies every byte against the committed lock before uploading it. A changed archive fails closed rather than replacing old data.

The scheduled `check-upstream` workflow checks locked upstreams weekly with the local download cache disabled and opens or updates an issue when an upstream becomes unavailable or returns different bytes. It never modifies an existing release automatically.

## Cloudflare R2 setup

Create a bucket, for example `htsvoice-repo`, and connect `htsvoice-repo.alcnaka.com` under **R2 > Bucket > Settings > Public access > Custom Domains**. Keep the `r2.dev` development URL disabled for production.

Configure a Cloudflare Cache Rule for `htsvoice-repo.alcnaka.com/v1/blobs/*` and `/v1/sources/*` so non-standard `.htsvoice` files are cacheable. The deployer also sets:

- immutable objects: `Cache-Control: public, max-age=31536000, immutable`
- JSON metadata: `Cache-Control: public, max-age=300`

GitHub Actions secrets:

```text
R2_ACCOUNT_ID
R2_BUCKET_NAME
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
```

The R2 token only needs **Object Read & Write** access scoped to this bucket. Immutability is primarily enforced by content-addressed keys, committed SHA-256 locks, and the publisher's HEAD-before-PUT behavior. **Do not use indefinite Bucket Lock by default**: a legal/licensing withdrawal may require removing a public object, and an indefinite lock would prevent that. If you want protection against accidental deletion, use a finite retention window only after deciding on a withdrawal policy, or replicate immutable artifacts to a separate private archival bucket.

## Initial voices

- `mei`: MMDAgent Example 1.8, 5 styles
- `takumi`: MMDAgent Example 1.8, 4 styles
- `tohoku-f01`: ICN Lab / Tohoku University, 4 emotions; upstream is pinned to commit `8e3306021db135c265f5eda5f062dc489707ddf8`
- `nitech-m001`: Open JTalk / HTS Working Group

All initial entries are intentionally `pending` until their upstream archives are fetched and `lock.json` files are committed.

## Commands

```bash
python tools/registry.py validate
python tools/registry.py validate --strict
python tools/registry.py lock <voice> <release>
python tools/registry.py check-upstream <voice> <release>
python tools/registry.py build-metadata --out dist
python tools/registry.py stage-release <voice> <release> --out dist
```

## Repository rules worth enabling

Protect `main`, require the `validate` workflow, and review changes under `voices/**`. Treat changes to an existing `lock.json` entry as exceptional: normal updates should append a new release/revision instead.

## Repository license

The original code, scripts, schemas, configuration, and documentation in this repository are licensed under the MIT License. See [LICENSE](./LICENSE).

Mirrored HTS voice models, upstream source archives, and license snapshots are **not** relicensed under MIT. They remain subject to their respective upstream licenses and attribution requirements as recorded in each voice metadata entry and published license snapshot.

