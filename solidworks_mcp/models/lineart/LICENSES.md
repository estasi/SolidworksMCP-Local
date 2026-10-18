# Line-art model provenance and license notes

This GitHub source publication excludes both the optional weight binaries and
their former Git LFS pointer files. The queue/core installation does not require
these models. Their exact converted binaries remain excluded until conversion
provenance and redistribution permissions have been established. Pointer and
object identities are preserved below for attribution and traceability.

## Omitted pointer identities

The table records the exact pointers retained in the predecessor source snapshot.
Neither these pointer files nor their referenced objects are included in this
GitHub tree. The publication manifest provides the same omission records.

| Original path | Pointer bytes | Pointer SHA-256 | LFS object SHA-256 | Expected weight bytes |
| --- | ---: | --- | --- | ---: |
| `solidworks_mcp/models/lineart/dexined_biped_v2.pt` | 134 | `e4a75b207186d755ce16f31d488a68bbb4c5174801f6e325b53e8f3992507993` | `096c2230d3060104bae083a4b0c882ebc6301d730c401c7e8556a6d8f944c65b` | 141291499 |
| `solidworks_mcp/models/lineart/teed_biped_5.pt` | 131 | `9b1159c5fb80543873f887e4423250e3fca7357e53ac874ad17c8a0475e88e7d` | `3175ec531b2238eaaacbcfac17111666f2b8c885bd04f785de36f8c776c80c3a` | 296344 |

The inherited backend identifies the following TorchScript inference exports:

| Export | Expected SHA-256 | Source project |
| --- | --- | --- |
| `dexined_biped_v2.pt` | `096c2230d3060104bae083a4b0c882ebc6301d730c401c7e8556a6d8f944c65b` | [DexiNed](https://github.com/xavysp/DexiNed), BIPED v2 checkpoint reference |
| `teed_biped_5.pt` | `3175ec531b2238eaaacbcfac17111666f2b8c885bd04f785de36f8c776c80c3a` | [TEED](https://github.com/xavysp/TEED), BIPED checkpoint 5 reference |

These checksums identify the binaries expected by the current code. They do not
establish how the binaries were converted or grant license rights.

The source projects carry these MIT code notices:

- DexiNed: copyright (c) 2019 Xavier Soria Poma;
  [upstream license](https://github.com/xavysp/DexiNed/blob/master/LICENSE).
- TEED: copyright (c) 2022 Xavier Soria Poma;
  [upstream license](https://github.com/xavysp/TEED/blob/main/LICENSE).

Both projects document training with BIPED. The
[dataset author's license section](https://xavysp.github.io/MBIPED/#license)
limits dataset use to non-commercial purposes. That dataset condition alone
does not determine the legal status of trained weights. The code licenses and
dataset terms do not, by themselves, settle permission to redistribute these
exact converted checkpoints. No unrestricted commercial redistribution clearance
for them is claimed here. The retained MIT notice below covers the cited code
attribution; it is not a replacement license for datasets or weight binaries.

For SAM 2.1, ViTMatte and the optional dependency boundaries, see
[THIRD_PARTY_NOTICES.md](../../../THIRD_PARTY_NOTICES.md).

## Retained MIT notice

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
