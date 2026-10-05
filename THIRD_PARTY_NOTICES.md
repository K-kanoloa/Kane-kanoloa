# Third-Party Notices

Kane-Kanaloa's own license status remains UNLICENSED. This notice preserves
dependency attribution and does not relicense third-party software.

Kanaloa Harness is an optimized/modified DSH-based runtime, not stock DSH.
Upstream DSH packages are Copyright (c) 2026 DeepSeek, MIT licensed.
`@deepseek-ai/dsh` and `@deepseek-ai/dsh-acp` are pinned to `0.1.5-rc.3`.
The ACP patch script modifies the installed dependency for Kanaloa without
removing upstream LICENSE files. Installed @deepseek-ai packages were checked
to retain LICENSE files during release preparation.

Other dependencies are installed, not vendored in this source release:
Next.js, React, React DOM, FastAPI and Pydantic (MIT); Playwright (Apache-2.0);
HTTPX and Uvicorn (BSD-3-Clause); pytest (MIT); Connector dependencies including
keyring (MIT) and websockets (BSD-3-Clause). Transitive dependencies retain their
own licenses. Preserve package-manager LICENSE/NOTICE files when distributing
binaries or offline dependency bundles. node_modules and Python environments
are excluded from this source release.

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
