# Contributing

## Branching (trunk-based)

- `main` is protected and always deployable. No direct pushes.
- Create short-lived branches from `main`:
  - `feat/<area>-<short-desc>` e.g. `feat/item-presigned-upload`
  - `fix/<area>-<short-desc>`
  - `infra/<short-desc>`, `docs/<short-desc>`
- Keep branches under 2 days old; rebase on `main` before opening the PR.

## Commits

Conventional Commits: `feat(item): add presigned upload URL`, `fix(matching): ...`, `infra: ...`, `docs: ...`, `test: ...`.

## Pull requests

1. Fill in the PR template (what, why, how tested).
2. CI must be green (tests, Trivy scan, image build).
3. At least 1 approval from another member.
4. Squash-merge, delete the branch.

## Effort log (needed for progress reports and the final report)

Log your hours in the PR description (`Effort: 3h`) or in the team sheet. We compare Seed 1 and Seed 2 effort to prove platform reuse.

## Secrets

Never commit credentials, `.env` files or AWS keys. CI uses GitHub OIDC to assume an AWS role; local work uses your own AWS profile.

## New service

Copy `services/service-template` to `services/<name>`, rename, keep the `/health` endpoint, Dockerfile and tests. CI picks it up automatically.
