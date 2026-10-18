# GitHub publication review — 2026.10.18.6

This is a source-only publication package of the **accepted `.6` part-targeting
runtime** for [estasi/SolidworksMCP-Local](https://github.com/estasi/SolidworksMCP-Local),
tag `v2026.10.18.6`. Native acceptance and full offline verification apply to the unchanged exact runtime;
see [RELEASE_VALIDATION.md](../RELEASE_VALIDATION.md).

This public copy updates README/validation notes after acceptance. All 75 Python
files remain identical to the native-tested source and commit. The publication
manifest records every documentation transform, the original canonical MIT bytes,
and every included payload hash. The final tree and ZIP receive a new exact
content/privacy/license/link audit. Private native reports and runtime state are
excluded. Historical `.5` native and test-cleanup failures remain retained; they
are not represented as current `.6` failures or retroactively resolved.

## Contents and provenance

The source tree retains project source, tests, documentation, dependency
requirements and MIT attribution. The two former Git LFS pointers are omitted;
their original paths, pointer hashes, object identities and sizes are retained
in the publication manifest and model notices. Native code derives from qualified `.4`
commit `5862415ba43bc2586a764459ac9821eefb804f3d`; `.5` adds opt-in queued document
selection and its tests. Publication documentation, dependency/model notices
and constraints are retained from the reviewed `.4` preparation. The final `.6`
manifest, rather than the predecessor commit, identifies the published contents.
See [DOCUMENT_TARGET.md](DOCUMENT_TARGET.md) for selection constraints and the
separate native acceptance gate. No new dependency or model assets were added.

The attribution chain is:

1. [alisamsam/Solidworks-MCP](https://github.com/alisamsam/Solidworks-MCP), original
   author Samsaam Ali Baig, copyright notice retained verbatim in [LICENSE](../LICENSE).
2. [czuryk/SolidworksMCP](https://github.com/czuryk/SolidworksMCP), imported baseline
   `38468b32890a9fc4c4aeec50fa97f679496c4524` (upstream 6.5.31).
3. Local SW2025 reliability corrections, then durable queue and process-guard work.
   `6.6.0-queue.1` was only a provisional local candidate name.

A new source snapshot preserves these credits without copying local Git metadata.
Use a reviewed public/noreply identity for any new publication commits. Do not push
the existing local branches or mirror their history to a public remote.

## Privacy review

The historical `.4` baseline review covered 86 tracked files, all 9 reachable commits and 159
unique blobs, including commit metadata and references. Selected credential,
PEM, normalized/escaped user-profile and network-path patterns were checked;
URLs, contact candidates, fixtures, binary files and LFS pointers were triaged.
No confirmed embedded credential, customer CAD file, private contact or workstation
path was found in the source content. Generic installation/test paths are examples.

Seven local commits contain personal author/committer metadata. The local remote
also points to a filesystem checkout. Both are excluded by exporting an explicit
source allowlist **without `.git`**, rather than publishing the development repo.
Private audit evidence remains outside the exported tree. Pattern screening and
manual triage reduce risk; they are not proof that every possible confidential
value has been recognized. Scan the final file set again after any later edits.

The export must exclude runtime/configuration backups, journals/SQLite sidecars,
endpoint credentials, launch receipts, recovery bundles, CAD documents/exports,
logs, screenshots, virtual environments, caches, wheels, DLLs and model binaries.
A `.gitignore` entry alone does not remove existing tracked or historical content.

## Licenses and third-party material

The root MIT notice is unchanged. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)
records the two upstreams, the clean core dependency inventory and optional
component boundaries. Dependencies are not all MIT; for example certifi is
MPL-2.0, while native wheels can include additional component licenses.

Only source is distributed. Dependencies installed by the operator retain their
own licenses. A future bundled environment, executable, wheel cache or installer
requires an inventory of its actual binaries, license texts and any applicable
source-availability obligations. SOLIDWORKS and its proprietary templates/SDK
material are not included or licensed by this project.

The two formerly included `.pt` LFS pointer files are omitted from this GitHub
source tree. Their exact converted checkpoints, conversion provenance and
redistribution permissions have not been established.
BIPED's non-commercial dataset terms do not automatically determine the rights
of trained weights; neither does an MIT code license settle those rights. No
weight binaries, datasets or private model caches are included or uploaded.
See [model notices](../solidworks_mcp/models/lineart/LICENSES.md). This repository
does not serve those LFS objects, and core operation does not need them. Removing
the pointers also avoids publishing references to absent LFS objects; no LFS
integrity check is bypassed and no unreviewed weight binary is uploaded.

## Installation and validation

During `.4` preparation, a fresh Windows/Python 3.14 environment installed `requirements-queue.txt` from
public PyPI. Its 31-package core resolution is documented in the notices and
`constraints-core-py314-win.txt`. The existing CAD environment was not upgraded.
Optional scientific/GPU dependencies and model weights are separate.

The full inherited suite is not the core-only test command: a core-only run
retained 2 failures and 36 errors from unavailable optional functionality.
Queue and document-identity tests passed separately: 174 passed, one explicit
host-capability skip, zero native attempts. A second clean environment with the
same core versions plus five scientific extras passed the full guarded suite:
423 passed, one host-capability skip, zero errors/failures/native attempts; the
separate broker suite passed 9/9. Both failed core-only assertions now pass.
Exact scientific test constraints are in `constraints-testextras-py314-win.txt`;
no GPU packages or weights were installed. These test-only dependencies are not
included as binaries in the publication source.
The native `.4` station result, earlier failures and limits are recorded in
[QUEUE_VALIDATION.md](QUEUE_VALIDATION.md). No new CAD trial was performed during
`.4` publication preparation; no CI success is claimed before GitHub actually runs it.

The `.5` focused selection suite passed 47/47 and owner-pin checks passed 18/18.
Its final queue suite passed 213 with one host-capability skip (214 collected),
using synthetic COM and isolated queue
processes. These are not native `.5` acceptance. Retain the native result against
the exact release manifest before production use; assembly/drawing targeting
remains uncertified. The final `.6` public file set received its own privacy scan after
these edits. Source-only export still excludes private operational evidence.

The workflow in `.github/workflows/queue-tests.yml` requests Windows/Python
3.11 and 3.14 queue tests using core dependencies and LFS disabled. Its existence
is not evidence of a passing CI run. The optional GPU dependency ranges are not
promised to resolve on every supported core Python version.

## Publication procedure

1. Review the exact source snapshot and its per-file SHA256 manifest; keep private
   audit evidence and the development `.git` directory out of the upload.
2. Use the public destination `estasi/SolidworksMCP-Local` and a public/noreply Git
   author/committer identity; initialize fresh history from this reviewed snapshot.
3. Keep the README clone URL and release tag consistent with the destination.
   Rescan edited files and preserve original copyright, upstream links and notices.
4. Keep optional weight binaries and their LFS pointers excluded. Do not upload
   replacement checkpoints or claim optional model readiness.
5. Run the configured CI and inspect actual results. Test a clean checkout and
   any newly claimed native capabilities separately using synthetic documents.
6. Publish only after a concrete instruction to create/push the repository.

The existing `prepare_queue_release.py` exports clean committed HEAD for an
installation release; it does not sanitize author metadata or publish to GitHub.
This reviewed publication snapshot intentionally omits local Git history.
