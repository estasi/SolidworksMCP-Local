# Third-party notices and distribution scope

Reviewed on 2026-10-18 for the `.4` source publication; retained for SolidworksMCP
Local build `2026.10.18.6`, which adds no new dependency or model assets.
The dependency inventories and full-suite results below were measured for `.4`;
they do not certify native `.5` or `.6` document selection.

## Project attribution

This is an independently modified fork. Its immediate base is
[czuryk/SolidworksMCP](https://github.com/czuryk/SolidworksMCP), derived from
[alisamsam/Solidworks-MCP](https://github.com/alisamsam/Solidworks-MCP).
The imported base revision is `38468b32890a9fc4c4aeec50fa97f679496c4524`
(upstream version label `6.5.31`). The local build number is not an upstream release.

The original MIT notice is retained in [LICENSE](LICENSE), including
`Copyright (c) 2026 Samsaam Ali Baig`. Preserve that notice and the complete
permission/disclaimer text in copies or substantial portions of this project.
The [original license](https://github.com/alisamsam/Solidworks-MCP/blob/main/LICENSE)
and [immediate upstream license](https://github.com/czuryk/SolidworksMCP/blob/main/LICENSE)
were checked. Existing source author credits also remain attributable to their
authors; the local queue changes do not imply upstream endorsement.

## What this source distribution contains

This release distributes project source, tests, documentation, and dependency
requirements. It does not distribute Python, a virtual environment, dependency
wheels or DLLs, CUDA, SOLIDWORKS, model-weight binaries, or training datasets.
Those components have separate terms. The root MIT license does not relicense them.

Two previously retained Git LFS pointers and their corresponding weight binaries
are omitted from this GitHub source distribution. Their original paths, pointer
SHA-256 values, object identities and expected sizes remain documented in the
publication manifest and model notices. No corresponding LFS objects are uploaded.
Core queue operation does not require model downloads. See
[line-art provenance and license notes](solidworks_mcp/models/lineart/LICENSES.md).

## Core Python dependencies

The following inventory records the clean Windows/Python 3.14 installation
resolved on 2026-10-18 from `requirements-queue.txt`. It includes dependency extras
(in particular `PyJWT[crypto]`) and active transitive dependencies. Versions are an
observed resolution, not a cross-platform lock or a guarantee about future installations.
The four direct requirements are pinned. `constraints-core-py314-win.txt` also
pins this observed Windows/Python 3.14 transitive set; without those constraints,
transitive requirements can resolve differently. The existing CAD runtime was not changed for this inventory.

License labels below come from the distributions' metadata and included license
files. They summarize the Python projects, not every bundled native component.
Each linked package page identifies the separately obtained distribution.

| Package | Observed version | Declared project license |
| --- | --- | --- |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | `0.8.0` | MIT |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | `4.15.1` | MIT |
| [attrs](https://pypi.org/project/attrs/26.1.0/) | `26.1.0` | MIT |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | `2026.7.22` | MPL-2.0 |
| [cffi](https://pypi.org/project/cffi/2.1.1/) | `2.1.1` | MIT-0 |
| [click](https://pypi.org/project/click/8.5.0/) | `8.5.0` | BSD-3-Clause |
| [cryptography](https://pypi.org/project/cryptography/50.0.2/) | `50.0.2` | Apache-2.0 OR BSD-3-Clause |
| [h11](https://pypi.org/project/h11/0.16.0/) | `0.16.0` | MIT |
| [httpcore](https://pypi.org/project/httpcore/1.0.9/) | `1.0.9` | BSD-3-Clause |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | `0.28.1` | BSD-3-Clause |
| [httpx-sse](https://pypi.org/project/httpx-sse/0.4.3/) | `0.4.3` | MIT |
| [idna](https://pypi.org/project/idna/3.20/) | `3.20` | BSD-3-Clause |
| [jsonschema](https://pypi.org/project/jsonschema/4.26.0/) | `4.26.0` | MIT |
| [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/2025.9.1/) | `2025.9.1` | MIT |
| [mcp](https://pypi.org/project/mcp/1.30.0/) | `1.30.0` | MIT |
| [pillow](https://pypi.org/project/pillow/12.3.0/) | `12.3.0` | MIT-CMU |
| [pycparser](https://pypi.org/project/pycparser/3.11/) | `3.11` | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/2.14.1/) | `2.14.1` | MIT |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/2.16.0/) | `2.16.0` | MIT |
| [pydantic_core](https://pypi.org/project/pydantic_core/2.50.1/) | `2.50.1` | MIT |
| [PyJWT](https://pypi.org/project/PyJWT/2.15.1/) | `2.15.1` | MIT |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.4/) | `1.2.4` | BSD-3-Clause |
| [python-multipart](https://pypi.org/project/python-multipart/0.0.32/) | `0.0.32` | Apache-2.0 |
| [pywin32](https://pypi.org/project/pywin32/312/) | `312` | PSF |
| [referencing](https://pypi.org/project/referencing/0.37.0/) | `0.37.0` | MIT |
| [rpds-py](https://pypi.org/project/rpds-py/2026.9.1/) | `2026.9.1` | MIT |
| [sse-starlette](https://pypi.org/project/sse-starlette/3.5.0/) | `3.5.0` | BSD-3-Clause |
| [starlette](https://pypi.org/project/starlette/1.8.0/) | `1.8.0` | BSD-3-Clause |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | `0.4.4` | MIT |
| [typing_extensions](https://pypi.org/project/typing_extensions/4.16.0/) | `4.16.0` | PSF-2.0 |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | `0.54.0` | BSD-3-Clause |

In particular:

- `certifi` contains Mozilla certificate data under MPL-2.0. Do not describe the
  complete installed dependency set as MIT-only.
- `pywin32` reports PSF metadata but ships several distinct notices: its Windows
  and COM modules use BSD-style terms; Python/IDLE, Scintilla, Microsoft MAPI and
  `adodbapi` have their own notices. The latter includes LGPL-2.1. See the
  [upstream license files](https://github.com/mhammond/pywin32) and
  [adodbapi license](https://github.com/mhammond/pywin32/blob/main/adodbapi/license.txt).
  This project imports Windows/COM interfaces, not `adodbapi`, but redistributing
  an entire wheel or environment would also redistribute its included components.
- `Pillow` uses MIT-CMU for the project and carries separate codec/font-library
  notices in its distributed LICENSE. `cryptography` wheels also have native
  dependency terms. Preserve the exact wheel notices if distributing binaries.

No dependency binaries are copied into this source release. A future standalone
installer, wheel bundle or executable needs a new inventory of its actual files,
their full license texts, and any applicable source-availability obligations.

## Optional raster and deep-learning dependencies

`requirements.txt` describes a broader optional environment. Its ranges are not
fully pinned and are not the tested core installation recipe. Optional model
backends and their complete dependency resolution are not certified by the core
queue acceptance tests.

| Dependency | Project license summary | Additional scope |
| --- | --- | --- |
| NumPy | BSD-3-Clause plus component notices | Native wheels include additional mathematical/runtime components. |
| SciPy | BSD-3-Clause plus component notices | Preserve wheel-specific bundled-library notices. |
| opencv-python-headless | MIT packaging; Apache-2.0 OpenCV | [Upstream licensing](https://github.com/opencv/opencv-python#licensing) describes FFmpeg under LGPL-2.1 and other bundled binaries. Headless wheels do not include the Qt GUI dependency. |
| scikit-image | Primarily BSD-3-Clause, with file-specific terms | Check its distributed LICENSE for the chosen version. |
| Shapely | BSD-3-Clause | Wheels bundle GEOS under LGPL-2.1 and additional platform notices; see [Shapely](https://github.com/shapely/shapely) and [GEOS licensing](https://libgeos.org/usage/download/). |
| Pillow | MIT-CMU plus bundled-component notices | Also a core dependency. |
| PyTorch | BSD-3-Clause and included notices | [PyTorch license](https://github.com/pytorch/pytorch/blob/v2.7.1/LICENSE); CUDA/native distributions introduce additional terms. |
| Transformers | Apache-2.0 | [License](https://github.com/huggingface/transformers/blob/v4.57.1/LICENSE). Model weights have their own terms. |
| huggingface-hub | Apache-2.0 | [License](https://github.com/huggingface/huggingface_hub/blob/v0.36.0/LICENSE). Downloading a model does not replace its license. |

This list describes the optional direct requirements and known bundled-license
boundaries. It is not an exhaustive software bill of materials for a future
GPU environment. A separate test environment installed the five scientific test
requirements to exercise the inherited offline suite. No GPU package or model
weights were installed; no dependency binaries are shipped in this publication.

## Model provenance and limits

| Model reference in the code | Primary-source terms and provenance | Publication status |
| --- | --- | --- |
| DexiNed, `dexined_biped_v2.pt` | [DexiNed code](https://github.com/xavysp/DexiNed) is MIT, copyright 2019 Xavier Soria Poma. The project documents BIPED training. The [BIPED dataset author](https://xavysp.github.io/MBIPED/) limits dataset use to non-commercial purposes. | Exact converted binary is not distributed. The dataset condition alone does not establish the license of trained weights. Conversion provenance and permission for redistribution of this exact artifact remain unresolved. |
| TEED, `teed_biped_5.pt` | [TEED code](https://github.com/xavysp/TEED) is MIT, copyright 2022 Xavier Soria Poma. Its README identifies BIPED training, with the same separate dataset condition. | Exact converted binary is not distributed; the same provenance and permission limit applies. |
| `facebook/sam2.1-hiera-large` | The [official model card](https://huggingface.co/facebook/sam2.1-hiera-large) declares Apache-2.0; the [SAM 2 project](https://github.com/facebookresearch/sam2) publishes code/checkpoint terms and notices. | Model weights, processor files and datasets are not bundled. The backend reads a separately populated local cache. |
| `hustvl/vitmatte-base-composition-1k` | The [model card](https://huggingface.co/hustvl/vitmatte-base-composition-1k) declares Apache-2.0 and says it was written by Hugging Face. The [original ViTMatte code](https://github.com/hustvl/ViTMatte/blob/main/LICENSE) is MIT. Its [training instructions](https://github.com/hustvl/ViTMatte/blob/main/docs/train.md) identify Adobe Image Matting and COCO data. | Weights are not bundled. The model-card label is not a review of training-data permissions; obtain and review the applicable dataset/checkpoint permissions before commercial deployment or redistribution. No commercial-rights clearance is claimed here. |

No datasets, training images, example photographs or converted model binaries
were added for this release. Retained source references and checksums are for
attribution and integrity; they do not substitute for license permission.

## SOLIDWORKS and platform software

SOLIDWORKS is proprietary software from Dassault Systemes and requires a separate
valid installation and license. This independent MCP integration does not include
or grant a SOLIDWORKS license and is not an official Dassault Systemes product.
Windows, Python and GPU components are obtained separately under their own terms.

## Scientific test environment

The full mocked regression suite was checked in a separate Windows/Python 3.14
environment using the same 31 pinned core distributions and these 10 additional
distributions. Five are direct test requirements; five are transitive. The
source publication includes requirements/constraints, not their wheels or DLLs.
No GPU dependencies, model weights or training data were installed for this check.
The operating CAD environment was not changed.

| Distribution | Tested version | Role | Project license summary |
| --- | --- | --- | --- |
| [NumPy](https://pypi.org/project/numpy/2.5.4/) | 2.5.4 | Direct | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (distribution metadata) |
| [SciPy](https://pypi.org/project/scipy/1.18.1/) | 1.18.1 | Direct | BSD-3-Clause plus component-specific notices |
| [opencv-python-headless](https://pypi.org/project/opencv-python-headless/5.0.0.93/) | 5.0.0.93 | Direct | Apache-2.0 for OpenCV; MIT packaging; bundled-component notices |
| [scikit-image](https://pypi.org/project/scikit-image/0.26.0/) | 0.26.0 | Direct | Primarily BSD-3-Clause, with file-specific terms in its LICENSE |
| [Shapely](https://pypi.org/project/shapely/2.2.0/) | 2.2.0 | Direct | BSD-3-Clause; bundled GEOS has separate LGPL-2.1 terms |
| [ImageIO](https://pypi.org/project/imageio/2.38.1/) | 2.38.1 | Transitive | BSD-2-Clause |
| [lazy-loader](https://pypi.org/project/lazy-loader/0.6/) | 0.6 | Transitive | BSD-3-Clause |
| [NetworkX](https://pypi.org/project/networkx/3.7/) | 3.7 | Transitive | BSD-3-Clause |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Transitive | Apache-2.0 OR BSD-2-Clause |
| [tifffile](https://pypi.org/project/tifffile/2026.10.15/) | 2026.10.15 | Transitive | BSD-3-Clause |

These are package-license summaries from the exact installed distributions, not
complete SPDX expressions for all native code inside their wheels. The bundled
FFmpeg/GEOS/native-library obligations described above still apply to any future
binary distribution. Test-environment dependencies are not shipped by this
source release.

The pip installation record retained privately is `clean-testextras-install.json`, SHA-256
`b9eb51dd3cb2a8ebc8016ca7614577405ae9ccffaaf28f8a50fdb25d69bc83c9`.
The per-package `install[].metadata.name`, `install[].metadata.version` and
`install[].download_info.archive_info.hashes.sha256` entries identify the exact
resolved artifacts. A sanitized inventory is retained privately as
`dependency-clean-testextras-inventory.json`. Reproduction constraints are
`constraints-testextras-py314-win.txt` (41 distributions, including the 31 core
ones). A constraints file records version choices; it is not a wheel hash lock.

Results: 423 passed and 1 skipped in the 424-test full suite, followed by 9/9
broker tests. `pip check` passed; both guarded runs recorded zero attempted
native CAD calls. This validates the mocked/synthetic suite and dependency
installation, not native raster/model backends. The earlier core-only full-suite
failure remains retained; the scientific dependencies above resolved those
missing-dependency cases without implementation changes by this reviewer.
