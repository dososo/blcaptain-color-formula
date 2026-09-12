# BLCaptain Visual Director & Color Formula Skill v4.9.2

[中文](README.md) · [English](README.en.md)

[![Release](https://img.shields.io/github/v/release/dososo/blcaptain-color-formula)](https://github.com/dososo/blcaptain-color-formula/releases/latest) [![Checks](https://github.com/dososo/blcaptain-color-formula/actions/workflows/ci.yml/badge.svg)](https://github.com/dososo/blcaptain-color-formula/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/License-MIT-blue)](LICENSE) ![Python](https://img.shields.io/badge/Python-3.9%2B-blue) ![Local first](https://img.shields.io/badge/Processing-Local_first-426953)

**A photo or video → understand the image, choose its color direction, approve a new result.**

[Download](https://github.com/dososo/blcaptain-color-formula/releases/latest) · [Start in three steps](#install) · [Explore all 62 comparisons](FORMULAS.en.md)

![BLCaptain 调色 Skill：先看懂画面，再决定色彩](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/hero-editorial.png)

<sub>AI 生成宣传主视觉；不是调色前后实测。实际效果请看下方同源对比。</sub>

| Unique formulas | Photos | Standard SDR video | Original protection |
| :---: | :---: | :---: | :---: |
| **32** | **31** | **31** | **Never overwritten** |

## See it in 48 seconds

[Watch landscape video (16:9)](https://github.com/dososo/blcaptain-color-formula/releases/download/v4.9.2/blcaptain-color-formula-4.9.2-landscape.mp4) · [Watch portrait video (9:16)](https://github.com/dososo/blcaptain-color-formula/releases/download/v4.9.2/blcaptain-color-formula-4.9.2-portrait.mp4)

Real photo transformations, multiple looks from one source, moving video comparisons, the approval workflow, and the full formula overview. Both versions include continuous background music and action sound effects. [Media and music credits](https://github.com/dososo/blcaptain-color-formula/blob/main/showcase/VIDEO_CREDITS_4.9.2.md).

## What it is

Give Codex a photo or standard SDR video. It reads the image, recommends up to three suitable directions, and also lets you choose directly from the full catalog. It renders locally only after you approve the current `plan_id`. **The original is never overwritten.**

This is a Codex Skill for rendering graded photos and videos, or for explaining manual adjustments in familiar editing tools. Its 32 formulas include 21 general directions and 11 original BLCaptain Signature styles. Every declared formula-media path can be selected, planned, and executed.

## What it helps you do

| Your goal | What BLCaptain does | What you receive |
| --- | --- | --- |
| Find a direction for an image | Reads exposure, white balance, subject, and mood; recommends up to three directions | A reasoned choice with less guesswork |
| Add character while preserving skin and detail | Builds the foundation before applying the look and checking skin, blacks, and whites | More controlled color changes |
| See what the grade actually changed | Creates a result and comparison from the same source | A genuine side-by-side comparison |
| Refine a result you do not like | Creates a revised plan from the original and waits for approval | A new output with the original preserved |

For photography enthusiasts, travel and portrait creators, short-video creators, and Codex users who want to work in natural language. Beginners can describe a feeling; experienced users can select formulas, adjust strength, or request manual editing steps.

> This is a public beta. Each source is checked for suitability; skin, clipping, or black/white-point risks may require a different direction or lower strength. See the [supported scope](references/public-beta-scope.md).

## Install

Verified on macOS with Python **3.9+** and FFmpeg/ffprobe. Windows and Linux cold installation has not yet been verified.

1. Download `blcaptain-color-formula-4.9.2.zip` and its `.sha256` file from the [latest release](https://github.com/dososo/blcaptain-color-formula/releases/latest).
2. Unzip and run:

```bash
python3 scripts/install_skill.py
```

3. Restart Codex, attach a photo, and say:

```text
Use BLCaptain Color Formula on this photo. Offer up to three suitable directions, wait for my approval, and do not overwrite the original.
```

The installer defaults to `~/.codex/skills/blcaptain-color-formula` and refuses to overwrite an existing installation. The core photo pipeline uses the Python standard library plus FFmpeg. Optional semantic features are listed in `requirements-optional.txt`; installing dependencies alone does not download model weights.

If you do not use a terminal, give Codex the extracted folder and ask: “Check Python and FFmpeg, then run this folder's installer. Tell me first if an older installation exists.” Restart Codex afterward.

You can also clone the repository into an unused Skill directory:

```bash
git clone https://github.com/dososo/blcaptain-color-formula.git ~/.codex/skills/blcaptain-color-formula
```

## Your first photo

The flow is diagnosis → up to three recommendations → one current plan and `plan_id` → approval → graded result, before/after comparison, palette, and receipt. You may also select any compatible formula directly by ID.

An `executable` recommendation means only that this source passed same-chain preflight. Use `refine` to create a new direction from the original. If any result, comparison, palette, or receipt step fails, the whole group is rolled back.

<details>
<summary>Terminal workflow: photos, video, and local adjustments</summary>

### Photo

```bash
python3 scripts/blcaptain_color.py suggest --input /path/photo.jpg --mode smart --count 3 --strength 55 --display-only
python3 scripts/blcaptain_color.py plan --input /path/photo.jpg --style selected-formula-id --strength 55 --output-dir /output --plan-out /output/plan.json
python3 scripts/blcaptain_color.py render --plan /output/plan.json --confirm-plan current-plan-id
python3 scripts/blcaptain_color.py refine --input /path/photo.jpg --current-style selected-formula-id --feedback "restore skin tone" --strength 0.55
```

`--strength 55` and `--strength 0.55` both mean 55%. If a plan fails safety checks, choose another direction, lower the strength, or use a more suitable source.

### Video

```bash
python3 scripts/blcaptain_color.py suggest --input /path/video.mp4 --mode smart --count 3 --strength 55 --display-only
python3 scripts/blcaptain_color.py shots --input /path/video.mp4
python3 scripts/blcaptain_color.py plan --input /path/video.mp4 --style selected-formula-id --strength 55 --shot-grade --confirm-shot-boundaries --output-dir /output --plan-out /output/video-plan.json
python3 scripts/blcaptain_color.py render --plan /output/video-plan.json --confirm-plan current-plan-id
```

For video, inspect shot boundaries before planning and watch the final continuous output with sound. A short preview or still frame is not a substitute for full playback.

For semantic local adjustments, use `plan --detect-local` to inspect actual candidates for this source. Review class, coverage, and edge risks, then explicitly use `render --confirm-local <strategy>` when rendering.

</details>

## Full catalog: 31 photo formulas and 31 video formulas

There are 32 unique formulas. `french-warm` is photo-only, `night-black-gold` is video-only, and the other 30 support both media types.

### Photo formulas (31)

`natural-clean`, `cream-soft`, `korean-cool`, `japanese-airy`, `french-warm`, `film-soft`, `forest-cyan`, `sunset-warm`, `cinematic-muted`, `teal-orange`, `food-vivid`, `landscape-crisp`, `night-cool-neon`, `warm-cozy`, `documentary-low-color`, `flash-ccd`, `rainy-blue-green`, `cool-gray-sea`, `blue-hour`, `bw-documentary`, `captain-deep-sea`, `celadon-forest`, `silver-morning-mist`, `paper-moon-bw`, `plateau-sacred-light`, `amber-afterglow`, `vermilion-snow-dream`, `obsidian-gold-realm`, `rain-ink-neon`, `desert-silent-rose`, `gilded-autumn-city`.

![31 photo formula comparisons](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/formula-atlas/photo-contact-sheet.jpg)

### Video formulas (31)

`natural-clean`, `cream-soft`, `korean-cool`, `japanese-airy`, `film-soft`, `forest-cyan`, `sunset-warm`, `cinematic-muted`, `teal-orange`, `night-black-gold`, `food-vivid`, `landscape-crisp`, `night-cool-neon`, `warm-cozy`, `documentary-low-color`, `flash-ccd`, `rainy-blue-green`, `cool-gray-sea`, `blue-hour`, `bw-documentary`, `captain-deep-sea`, `celadon-forest`, `silver-morning-mist`, `paper-moon-bw`, `plateau-sacred-light`, `amber-afterglow`, `vermilion-snow-dream`, `obsidian-gold-realm`, `rain-ink-neon`, `desert-silent-rose`, `gilded-autumn-city`.

![31 video formula comparisons](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/formula-atlas/video-contact-sheet.jpg)

[Open all 62 large comparisons with visual intent, evidence level, and source license](FORMULAS.en.md). “Human-accepted example” applies only to the exact source, strength, and result shown. “Public formula demo” shows direction on rights-cleared media and is not human aesthetic approval. Sources are selected for genuine grading headroom, never because they already match a formula name.

The atlas includes 13 human-accepted results (2 photo entries and 11 video entries) and 49 public formula demonstrations. Every comparison uses the same source before and after. Video stills compare color; full moving footage must be reviewed separately.

## What makes it different

- Foundation before Look: exposure, white balance, black and white points, overall color, and texture are checked first.
- Confirmation before rendering: an old `plan_id` cannot authorize a new source, strength, or local strategy.
- Per-source safety: all declared paths are available, but a plan is rejected if it damages black, white, skin, memory color, clipping, or change boundaries.
- Reversible output: originals are not overwritten and refinements restart from the source.
- Separate conclusions: automated checks, Codex color review, and human acceptance never impersonate one another.
- Honest demos: sources need visible grading headroom and are not selected to flatter a formula name.

## Media, privacy, and rights

Supported inputs are sRGB/Display P3 photos and standard SDR video. RAW, unidentified Log, HDR/PQ/HLG/Dolby Vision, parameter-identical cross-app matching, and arbitrary video semantic tracking are outside this release's guarantees.

The core pipeline reads media locally and writes to your selected output directory. There is no project account, telemetry, or project-operated upload server. Raw plans and receipts contain local paths and hashes; redact them before sharing. The feedback ledger defaults to `~/.blcaptain/feedback-ledger.json`; use `clear-feedback` to remove it.

You must have the right to process and publish your media. The MIT License covers project code and original documentation only, not FFmpeg, codecs, models, platforms, or user media. Read [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md), [source and data boundaries](references/source-and-data.md), and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Verification

The Python 3.9+ core runtime contract is separate from the Python 3.10.18 full development-test contract. The v4.9.2 Release is governed by its tagged CI, package verification, and clean-install evidence. Full tests apply only to the development repository; the Release Skill package excludes tests, historical evidence, user media, workspaces, atlas-build scripts, and Git metadata.

```bash
python3 -m unittest tests.test_v491_all_formulas_executable tests.test_public_formula_atlas tests.test_skill_package tests.test_portable_startup
python3 scripts/build_skill_package.py audit --root . --public-assets
```

## FAQ

**Will it overwrite my original?** No. Results go to a new output directory.

**Do I need to know terminal commands?** No. After installation, attach your media and describe the result you want. Expand the terminal workflow when you need precise control.

**Codex cannot find the Skill. What should I do?** Restart Codex and explicitly ask for BLCaptain Color Formula. If it still cannot find it, check that the installer's destination contains `SKILL.md`.

**Python or FFmpeg is missing. What next?** Ask Codex to check `python3 --version`, `ffmpeg -version`, and `ffprobe -version`, then install missing dependencies for your operating system and retry.

**Why are there 62 entries for 32 formulas?** Thirty formulas support both media types. French Warm is photo-only and Night Black Gold is video-only, so each medium has 31 entries.

**Why can an executable formula reject my source?** The formula is implemented, but this source and strength may damage skin, memory colors, blacks, whites, or clipping limits. Change the direction or strength.

**Does passing checks mean it looks good?** No. Technical checks, Codex review, and human acceptance are separate conclusions.

**Can it work offline?** Core photo processing runs locally. Network use by optional semantic backends depends on the implementation and models you enable.

## Author and contact

- BLCaptain NEXT
- GitHub: [@dososo](https://github.com/dososo)
- X: [@thinkszyg](https://x.com/thinkszyg)
- Email: [blteam2026@outlook.com](mailto:blteam2026@outlook.com)
- Feedback: [GitHub Issues](https://github.com/dososo/blcaptain-color-formula/issues)

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing. Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © BLCaptain. Third-party names are used only for factual identification and do not imply endorsement.
