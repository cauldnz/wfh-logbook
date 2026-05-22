# Contributing

Thanks for your interest. This project is primarily a personal tool maintained by the original author, but contributions and forks are welcome.

## Scope

This project is intentionally narrow:

- **In scope**: features that improve the contemporaneity, immutability, or auditability of the WFH record; support for additional UniFi controller versions; bug fixes; documentation improvements; ports to other network vendors *as separate adapters* behind a common interface.
- **Out of scope**: features that turn this into a general-purpose time tracker, productivity tool, payroll integration, or anything that requires sending data off-box by default. Anything that obscures or makes harder the production of a defensible audit trail.

## Tax-related guidance

This project provides a record-keeping tool, not tax advice. Pull requests that introduce claims about ATO rules, deductibility, or what is and is not allowable must:

1. Cite the relevant ATO publication or ruling (PCG, TR, etc.).
2. Be framed as record-keeping support, not as advice.
3. Be reviewed by the maintainer before merge.

The README and methodology document carry an explicit disclaimer; any new user-facing text touching tax matters must remain consistent with that disclaimer.

## How to contribute

1. Open an issue describing the change before opening a PR for anything non-trivial.
2. Keep PRs focused — one logical change per PR.
3. Update the spec (`HANDOFF.md` or the architecture doc) *before* changing code if the change alters behaviour described there. Spec-first is how this project is built.
4. Include tests for any new sessionisation logic — that code is the heart of the audit defensibility.
5. Update `CHANGELOG.md` under `## [Unreleased]`.

## Code of conduct

Be decent. Disagree with ideas, not people. The maintainer reserves the right to close issues or PRs that are abusive, off-topic, or attempt to weaponise the project for purposes contrary to its stated scope.

## Licence

By contributing you agree your contributions will be licensed under the MIT licence (see `LICENSE`).
