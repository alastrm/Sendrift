# Repository publication review

Reviewed on **2026-10-17**, starting at commit `3f00603f0a7e6965d3e45a167f451db31fe3c72e`. Scope: publishing the source as a local portfolio project, not operating a public service.

## Files and history

- Inspected the initial 89 tracked paths and all locally available Git refs. The four reachable commits contain 127 unique file blobs: 126 text versions and one bundled font.
- Scanned those text versions for private keys, common provider tokens, AWS access IDs, JWTs, credential URLs, secret assignments and high-entropy quoted values. Dependency lockfile integrity hashes were excluded from the entropy heuristic.
- Reviewed the reported assignments and credential URLs. They were synthetic test values, documented local credentials or environment-variable interpolation. No actual access secret was identified by these checks.
- No real `.env`, database dump, uploaded document, build directory, dependency directory or log was found in the tracked history. The font is accompanied by its license.
- Checked the current local environment files without copying their contents into this report. Credential fields examined contained the documented local placeholders. Git remote URLs have no embedded credentials.
- The updated documentation and new screenshot assets were reviewed separately. Original screenshots supplied in chat were not copied: they contain a capability URL, management key and personal filename. Replacement screenshots use a synthetic file, with secrets replaced before capture. The PNG files contain no text or EXIF metadata.

These checks found no secret requiring rotation or history removal. This is a bounded review, not a guarantee that every possible secret format or sensitive fact has been detected.

## Ignore rules and generated files

`.env*` is ignored except `.env.example`. Checks confirmed that local environment files, `node_modules`, `.next`, Python virtual environments, caches and TypeScript build metadata are ignored. The tracked `apps/web/app/lib/format.ts` helper remains included despite the general Python `lib/` rule.

Additional ignore rules cover local AWS/SSH directories, key containers, logs and common database export formats. These rules help prevent accidental additions; they do not remove anything already committed.

No unnecessary generated build output was found among tracked files. Local caches, installed dependencies and user data were left in place. `package-lock.json` and Next.js's small `next-env.d.ts` declaration file are retained intentionally.

## Local configuration

- Both Compose files now publish services on `127.0.0.1` only. The template uses host PostgreSQL port 5433; containers still use `postgres:5432`.
- The alternate `infra/compose.yaml` entry point now also carries the existing root configuration's build-time API proxy target, public-origin setting and disabled HTTP probe for the non-HTTP worker.
- Both configurations were rendered with `.env.example` and compared. They resolve to equivalent service settings and five loopback-only port mappings.
- The root stack was recreated without rebuilding images or removing volumes. Inspection confirmed loopback bindings and the same named PostgreSQL/MinIO volumes. The existing migration is `0001 (head)`.

## Checks performed

| Check | Result |
| --- | --- |
| API `pytest -q` | 97 passed, 48 skipped |
| Ruff | Passed |
| TypeScript typecheck | Passed |
| Frontend lint | Passed |
| Node proxy configuration tests | 4 passed |
| Next.js production build | Passed |
| Both Compose configurations with example environment | Valid; equivalent settings, loopback ports |
| Proxied `/health/ready` | HTTP 200; PostgreSQL and storage connected |
| Host-run Node.js → `localhost:8000/health/ready` | HTTP 200; local development proxy target remains reachable |
| Running-stack Docker smoke tests | 2 passed; includes 1 byte, 1 MiB and 100 MiB round trips, private storage and eventual cleanup |
| Real browser flow | Picker → upload → separate recipient context → byte-for-byte download; no runtime errors |
| Screenshot review | Three images inspected; synthetic filename, secrets redacted, no text/EXIF metadata |
| `git diff --check` | Passed |

The destructive/disposable-resource integration suite was not enabled. No fresh-volume rebuild or full installation on another machine was performed. The installed dependencies and existing local images were used; no dependency installation or upgrade was performed during this task.

## Before publishing

No confirmed credential leak blocks publication based on the local material examined. Two owner decisions remain:

1. Git history contains one author email address that is not a GitHub noreply address. Confirm that this address is intended to be public. History was not rewritten.
2. No application source license is present. Choose one if reuse is intended; none was added on the owner's behalf.

Do not publish the original chat screenshots or local environment files. Review the final Git diff before committing. No commit, push or GitHub visibility change was made during this preparation.

The earlier [security audit](../SECURITY_AUDIT.md) remains applicable to its audited snapshot and Internet-deployment threat model. Public source visibility does not resolve its dependency, logging or abuse-control findings. No public hosting is planned, and this work is not a production security sign-off.

## Review limits

The history scan covers objects reachable from local refs, including the locally available `origin/main`; it does not inspect remote-only refs, dangling objects, deleted remote branches, forks, GitHub Actions artifacts, releases, issues, attachments or externally retained copies. No provider-side credential validity checks were attempted. Binary font contents were not examined for steganography. Screenshot metadata and visible content were checked, not arbitrary hidden pixel encodings. New commits and files added after this review need their own review.
