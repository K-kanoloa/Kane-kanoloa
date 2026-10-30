# Third-Party Notices

Kane-Kanaloa's own code is licensed under PolyForm Noncommercial 1.0.0;
see LICENSE. This notice preserves dependency attribution and does not
relicense third-party software.

Kanaloa Harness is an optimized/modified DSH-based runtime, not stock DSH.
Upstream DSH packages are Copyright (c) 2026 DeepSeek, MIT licensed.
`@deepseek-ai/dsh` and `@deepseek-ai/dsh-acp` are pinned to `0.1.5-rc.3`.
The ACP patch script modifies the installed dependency for Kanaloa without
removing upstream LICENSE files. Preserve the upstream LICENSE files.

Other dependencies are installed, not vendored in this source release:
Next.js, React, React DOM, FastAPI and Pydantic (MIT);
HTTPX and Uvicorn (BSD-3-Clause); Connector dependencies including
keyring (MIT) and websockets (BSD-3-Clause). Transitive dependencies retain their
own licenses. Preserve package-manager LICENSE/NOTICE files when distributing
binaries or offline dependency bundles. node_modules and Python environments
are excluded from this source release.

## Optional Kanaloa Python Tools

The optional Kanaloa Python Tool image also installs NVIDIA NeMo NOOA 0.0.10
(Apache-2.0, NVIDIA Corporation and affiliates) and IPython 9.6.0
(BSD-3-Clause, the IPython development team), with their own dependency licenses.
Their installed distribution LICENSE/NOTICE files must be retained when
redistributing the optional image. Their source is not vendored or modified in
this repository; integration code lives under `packages/kanaloa-tools`.

The locked image also installs certifi (MPL-2.0) and tqdm (MPL-2.0 AND MIT).
These distributions are unmodified; retain their license files and applicable
source-availability notices when redistributing a binary image. Their upstream
sources are https://github.com/certifi/python-certifi and https://github.com/tqdm/tqdm.
Keep the Python/Debian base image's copyright/license files as well. This Git
delivery publishes source integration code only, not the Tool image or wheels.

## DSH MIT License

MIT License

Copyright (c) 2026 DeepSeek

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
