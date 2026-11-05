# Bundled reference data for `app.auth`

## `breached-passwords.txt.gz`

The local breached-password list checked by `app.auth.breached` whenever a
password is set (bootstrap, password change, password reset; AUTH.md
"Passwords", SECURITY.md A1). The check is case-insensitive: the list holds
lower-cased entries and a candidate is lower-cased before lookup.

- **Content**: 72,985 distinct entries, one per line, sorted, UTF-8,
  gzip-compressed (about 460 KiB; about 1 MiB uncompressed). Only entries of
  12 to 128 printable characters are kept, because PerFin refuses shorter
  passwords by length anyway; that is what keeps a list built from more than
  two million common passwords this small.
- **Sources** (all from [SecLists](https://github.com/danielmiessler/SecLists),
  directory `Passwords/Common-Credentials/`, branch `master`, fetched
  2026-11-03 through `raw.githubusercontent.com`; the repository HEAD was
  `35d74839e66f44d116e694369d86a5bf07aaed6f`):

  | File | Lines | SHA-256 of the download |
  | --- | --- | --- |
  | `100k-most-used-passwords-NCSC.txt` (UK NCSC top 100k from HIBP) | 99,840 | `c2e5696882c603b76bb67a47ee970897e5a76fc4c3f5547abe3d0ca340c576e0` |
  | `xato-net-10-million-passwords-1000000.txt` (top 1M of the xato.net 10M dump) | 1,000,000 | `424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51` |
  | `Pwdb_top-1000000.txt` (top 1M of the pwdb collection) | 1,000,000 | `e9a88f67aafe65496682dc374559ee714e978bee50314767494c3e37a18c9fc8` |

- **Build**: `python3 -I scripts/build_breached_passwords.py
  backend/app/auth/data/breached-passwords.txt.gz
  100k-most-used-passwords-NCSC.txt xato-net-10-million-passwords-1000000.txt
  Pwdb_top-1000000.txt` (standard library only; reproducible: no file name
  and a fixed mtime in the gzip header). SHA-256 of the result:
  `aad8b9b3fe95e050996324aaf5c17b16c56aa89f54e64695cc24e7ff11b1acdb`.
- **Licence**: SecLists is distributed under the MIT License, Copyright (c)
  2018 Daniel Miessler. The password lists are compilations of passwords
  published in public breach corpora; they contain no PerFin user data. The
  MIT notice is reproduced here as that licence requires:

  > Permission is hereby granted, free of charge, to any person obtaining a
  > copy of this software and associated documentation files (the
  > "Software"), to deal in the Software without restriction, including
  > without limitation the rights to use, copy, modify, merge, publish,
  > distribute, sublicense, and/or sell copies of the Software, and to permit
  > persons to whom the Software is furnished to do so, subject to the
  > following conditions:
  >
  > The above copyright notice and this permission notice shall be included
  > in all copies or substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
  > OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
  > MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
  > NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
  > DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
  > OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
  > USE OR OTHER DEALINGS IN THE SOFTWARE.

- **Why gzip**: the file is shipped inside the `app` package (the wheel and
  the image include it); compression keeps the repository and image small,
  and secret scanners (gitleaks) do not flag a list of common passwords.

To refresh the list, download the same files (or newer revisions), rebuild
with the command above, update the counts and checksums here, and run
`uv run pytest tests/unit/test_breached.py`.
