# Third-party notices

iAmped is licensed under the **GNU General Public License v3.0 or later**
(see [`LICENSE`](LICENSE)). It includes or bundles the following third-party
components, each under its own license. Their licenses are compatible with
distributing iAmped under GPL-3.0-or-later.

## Code included in this repository

| Component | License | Location |
|---|---|---|
| [audioMotion-analyzer](https://github.com/hvianna/audioMotion-analyzer) — © Henrique Vianna | AGPL-3.0 | `iamped/web/vendor/audiomotion-analyzer.js` · license: `iamped/web/vendor/AUDIOMOTION-LICENSE.txt` |
| [Butterchurn](https://github.com/jberg/butterchurn) — © 2013-2018 Jordan Berg | MIT | `iamped/web/vendor/butterchurn.min.js` · license: `iamped/web/vendor/BUTTERCHURN-LICENSE.txt` |
| [Butterchurn presets](https://github.com/jberg/butterchurn-presets) — © 2013-2018 Jordan Berg | MIT | `iamped/web/vendor/butterchurnPresetsMinimal.min.js` · license: `iamped/web/vendor/BUTTERCHURN-PRESETS-LICENSE.txt` |
| [wave.js](https://github.com/foobar404/wave.js) — © 2023 Austin Michaud | MIT | `iamped/web/vendor/wave.js` · license: `iamped/web/vendor/WAVEJS-LICENSE.txt` |
| [Font Awesome Free](https://fontawesome.com) icons — © Fonticons, Inc. | CC BY 4.0 | inline SVGs in `iamped/web/index.html` |
| Port of `itdb_hash58.c` from [libgpod](https://sourceforge.net/projects/gtkpod/) | LGPL-2.1-or-later | `iamped/sync/hash58.py` |

The audioMotion-analyzer code remains under the AGPL-3.0, which GPL-3.0
section 13 permits combining with GPL-3.0 code. The hash58 module remains
under LGPL-2.1-or-later.

## Python dependencies bundled into the portable builds

The macOS `.app` and Windows `.exe` freeze these packages (and their own
dependencies) into the executable:

| Package | License |
|---|---|
| [mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later |
| [Flask](https://github.com/pallets/flask) (+ Werkzeug, Jinja2, MarkupSafe, ItsDangerous, Click, Blinker) | BSD-3-Clause |
| [PlexAPI](https://github.com/pkkid/python-plexapi) | BSD-3-Clause |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU (HPND) |
| [Requests](https://github.com/psf/requests) (+ urllib3, idna, certifi, charset-normalizer) | Apache-2.0 (certifi: MPL-2.0; others MIT/BSD) |
| [Python](https://www.python.org) runtime | PSF License |

## Source code

The complete corresponding source for every release build is the tagged
commit in this repository, available from the release page.

## Not bundled

iAmped can call `ffmpeg`, `mpv`, and `fpcalc` (Chromaprint) if they are
installed on your system. They are separate programs that iAmped does not
distribute.
