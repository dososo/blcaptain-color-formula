# BLCaptain Visual Director & Color Formula Skill v4.9.1

[中文](README.md) · [English](README.en.md) · [All 62 formula-media comparisons](FORMULAS.en.md) · [Latest Release](https://github.com/dososo/blcaptain-color-formula/releases/latest) · [Issues](https://github.com/dososo/blcaptain-color-formula/issues)

Give Codex a photo or standard SDR video. It reads the image, recommends up to three suitable directions, and also lets you choose directly from the full catalog. It renders locally only after you approve the current `plan_id`. **The original is never overwritten.**

> v4.9.1 public beta contains 32 formal formulas: 31 for photos and 31 for video. Every declared formula-media path can be selected, planned, and executed. Each source still has to pass its own safety preflight; executable does not mean every source is suitable, and a technical pass is not aesthetic approval. See the [public beta scope](references/public-beta-scope.md).

## Install

Verified on macOS with Python **3.9+** and FFmpeg/ffprobe. Windows and Linux cold installation has not yet been verified.

1. Download `blcaptain-color-formula-4.9.1.zip` and its `.sha256` file from the [latest release](https://github.com/dososo/blcaptain-color-formula/releases/latest).
2. Unzip and run:

```bash
python3 scripts/install_skill.py
```

3. Restart Codex, attach a photo, and say:

```text
Use BLCaptain Color Formula on this photo. Offer up to three suitable directions, wait for my approval, and do not overwrite the original.
```

The installer defaults to `~/.codex/skills/blcaptain-color-formula` and refuses to overwrite an existing installation. The core photo pipeline uses the Python standard library plus FFmpeg. Optional semantic features are listed in `requirements-optional.txt`; installing dependencies alone does not download model weights.

## First photo and terminal flow

The flow is diagnosis → up to three recommendations → one current plan and `plan_id` → approval → graded result, before/after comparison, palette, and receipt. You may also select any compatible formula directly by ID.

An `executable` recommendation means only that this source passed same-chain preflight. Use `refine` to create a new direction from the original. If any result, comparison, palette, or receipt step fails, the whole group is rolled back.

```bash
python3 scripts/blcaptain_color.py suggest --input /path/photo.jpg --mode smart --count 3 --strength 55 --display-only
python3 scripts/blcaptain_color.py plan --input /path/photo.jpg --style selected-formula-id --strength 55 --output-dir /output --plan-out /output/plan.json
python3 scripts/blcaptain_color.py render --plan /output/plan.json --confirm-plan current-plan-id
```

For video, inspect shot boundaries before planning and watch the final continuous output with sound. A short preview or still frame is not a substitute for full playback.

## Full catalog: 31 photo formulas and 31 video formulas

There are 32 unique formulas. `french-warm` is photo-only, `night-black-gold` is video-only, and the other 30 support both media types.

### Photo formulas (31)

`natural-clean`, `cream-soft`, `korean-cool`, `japanese-airy`, `french-warm`, `film-soft`, `forest-cyan`, `sunset-warm`, `cinematic-muted`, `teal-orange`, `food-vivid`, `landscape-crisp`, `night-cool-neon`, `warm-cozy`, `documentary-low-color`, `flash-ccd`, `rainy-blue-green`, `cool-gray-sea`, `blue-hour`, `bw-documentary`, `captain-deep-sea`, `celadon-forest`, `silver-morning-mist`, `paper-moon-bw`, `plateau-sacred-light`, `amber-afterglow`, `vermilion-snow-dream`, `obsidian-gold-realm`, `rain-ink-neon`, `desert-silent-rose`, `gilded-autumn-city`.

![31 photo formula comparisons](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/formula-atlas/photo-contact-sheet.jpg)

### Video formulas (31)

`natural-clean`, `cream-soft`, `korean-cool`, `japanese-airy`, `film-soft`, `forest-cyan`, `sunset-warm`, `cinematic-muted`, `teal-orange`, `night-black-gold`, `food-vivid`, `landscape-crisp`, `night-cool-neon`, `warm-cozy`, `documentary-low-color`, `flash-ccd`, `rainy-blue-green`, `cool-gray-sea`, `blue-hour`, `bw-documentary`, `captain-deep-sea`, `celadon-forest`, `silver-morning-mist`, `paper-moon-bw`, `plateau-sacred-light`, `amber-afterglow`, `vermilion-snow-dream`, `obsidian-gold-realm`, `rain-ink-neon`, `desert-silent-rose`, `gilded-autumn-city`.

![31 video formula comparisons](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/formula-atlas/video-contact-sheet.jpg)

[Open all 62 large comparisons with visual intent, evidence level, and source license](FORMULAS.en.md). “Human-accepted example” applies only to the exact source, strength, and result shown. “Public formula demo” shows direction on rights-cleared media and is not human aesthetic approval. Sources are selected for genuine grading headroom, never because they already match a formula name.

## What makes it different

- Foundation before Look: exposure, white balance, black and white points, overall color, and texture are checked first.
- Confirmation before rendering: an old `plan_id` cannot authorize a new source, strength, or local strategy.
- Per-source safety: all declared paths are available, but a plan is rejected if it damages black, white, skin, memory color, clipping, or change boundaries.
- Reversible output: originals are not overwritten and refinements restart from the source.
- Separate conclusions: automated checks, Codex color review, and human acceptance never impersonate one another.
- Honest demos: sources need visible grading headroom and are not selected to flatter a formula name.

## Media, privacy, and rights

Supported inputs are sRGB/Display P3 photos and standard SDR video. RAW, unidentified Log, HDR/PQ/HLG/Dolby Vision, parameter-identical cross-app matching, and arbitrary video semantic tracking are outside this release's guarantees.

The core pipeline reads media locally and writes to your selected output directory. There is no project account, telemetry, or project-operated upload server. Raw plans and receipts contain local paths and hashes; redact them before sharing.

You must have the right to process and publish your media. The MIT License covers project code and original documentation only, not FFmpeg, codecs, models, platforms, or user media. Read [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md), [source and data boundaries](references/source-and-data.md), and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Verification

The Python 3.9+ core runtime contract is separate from the Python 3.10.18 full development-test contract. The v4.9.1 Release is governed by its tagged CI, package verification, and clean-install evidence. Full tests apply only to the development repository; the Release Skill package excludes tests, historical evidence, user media, workspaces, atlas-build scripts, and Git metadata.

## Author and contact

- BLCaptain NEXT
- GitHub: [@dososo](https://github.com/dososo)
- X: [@thinkszyg](https://x.com/thinkszyg)
- Email: [blteam2026@outlook.com](mailto:blteam2026@outlook.com)
- Feedback: [GitHub Issues](https://github.com/dososo/blcaptain-color-formula/issues)

## License

[MIT](LICENSE) © BLCaptain. Third-party names are used only for factual identification and do not imply endorsement.
