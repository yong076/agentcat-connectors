# WP37 — Reflect analysis quality report

WP37 removes duplicate analysis findings and makes analyzer coaching use the reader's language. `CONNECTOR_VERSION` remains `26.36.6`.

## What changed

- `reflect_validate_analysis` now merges friction and pattern entries with the same exact `(category, evidence)` pair. The first entry supplies the retained fields, counts are summed with a minimum contribution of 1 per entry, and the earliest numeric `turn` is retained. Distinct evidence remains a distinct finding.
- `reflect_present_analysis` applies the same merge to stored analysis JSON, so rows created before WP37 also render only once. Weekly and session summary calculations use the merged `count`, preserving occurrence totals.
- `reflect.json` now includes `"lang"`, one of `ko`, `en`, `ja`, or `zh-Hans`. A missing or invalid value resolves from macOS `AppleLocale` (`ko*` → `ko`, `ja*` → `ja`, `zh-Hans*` or `zh_CN` → `zh-Hans`, otherwise `en`); non-macOS systems and command failures use `en`. The generated default config stores the resolved value.
- `POST /reflect/analyze/{id}?lang=ko` and `agentcat reflect analyze <id> --lang ko` override the config language. Unsupported HTTP values return `400` with `error: "reflect_bad_request"`. The nightly scheduler resolves and uses the configured reader language.
- The analyzer prompt names the selected language for `note`, `after`, `working_style`, and `candidate_rules`, while requiring `evidence` and `before` quotes to remain verbatim in their original language. The analyzer JSON schema itself is unchanged.
- Analysis metadata has a backward-compatible SQLite `lang` column migration. A cached analysis is reused when its stored language matches the requested language or when it predates WP37 (no stored language); otherwise it is regenerated in the requested language.

## JSON fields added

- `count` — integer, minimum 1, added to every object in `analysis.frictions[]` and `analysis.patterns[]`. It is the number of findings merged into that one `(category, evidence)` row. Existing weekly finding `count` fields continue to represent occurrence totals.
- `lang` — one of `"ko"`, `"en"`, `"ja"`, or `"zh-Hans"`, added at `analysis.lang`, `analysis_meta.lang`, and synchronous analyze `meta.lang` for newly generated analyses. Legacy stored rows may present `null` until reanalyzed because their original reader language is unknown.

The app should multiply a finding's contribution by `count` and never repeat the finding row.

## Verification

- `HOME=$(mktemp -d) python3 -m unittest tests.test_reflect </dev/null` — **Ran 81 tests, OK**.
- `HOME=$(mktemp -d) python3 -m unittest discover -s tests -p '*test*.py' </dev/null` — **Ran 552 tests, OK**.

All Reflect test data remains synthetic, and subprocess-based locale detection is patched in tests so no real `defaults` command is used there.
