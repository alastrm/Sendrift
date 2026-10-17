# Sendrift security audit

Date: **2026-10-17**. Audited commit: `3f00603f0a7e6965d3e45a167f451db31fe3c72e`.

> **Portfolio scope update (2026-10-17):** Sendrift is now documented as a local-only portfolio project, with no public service planned. Subsequent Compose changes bind published ports to `127.0.0.1`. This report preserves findings against the audited commit, including its former all-interface bindings; it is not a description of the updated working tree or a deployment approval. Other findings have not been remediated by the documentation work.

## 1. Executive summary

**Overall deployment readiness: NOT READY for untrusted Internet users.**

The application has meaningful security controls: independent cryptographic bearer secrets, hash-only token storage, private object storage, API-mediated downloads, server-side expiration and revocation, bounded S3 transfers, and careful cleanup ownership. Small live tests confirmed these controls. They do not make the supplied deployment safe to publish.

The running Compose stack exposes backing services on every host interface and uses the documented development administrator credentials. Anonymous upload/download capacity has no aggregate abuse controls. The supplied edge example is HTTP-only and is not deployed by Compose. Next.js 14.2.35 is unsupported and matches multiple security advisories; Python 3.10 is also end-of-life. Capability URLs can escape redaction through proxy errors and the Nginx example's treatment of encoded paths.

This is an **audit, not a remediation**. No application source, configuration, dependency, container, or infrastructure was changed. No commits or pushes were made. This report is the only repository addition. Two tiny synthetic files were uploaded and revoked through the normal API; ordinary worker logs subsequently recorded their deletion. No existing user files were inspected or deleted.

Severity is based on the supplied deployment, with prerequisites stated explicitly. A matching dependency advisory is **not** presented as a successfully exploited vulnerability. In particular, no remote code execution, cross-user token bypass, or arbitrary internal-service access was demonstrated.

## 2. Trust boundaries and reachable surfaces

### Observed deployment

| Boundary | Observed surface | Consequence |
| --- | --- | --- |
| Client → Next.js | Host port 3000, bound to `0.0.0.0` and `::` | Pages, framework endpoints, `/api/*` and `/health/*` rewrites are reachable without Nginx. |
| Client → FastAPI | Host port 8000, all interfaces | Direct access bypasses any future controls applied only to the web proxy. |
| Client → PostgreSQL | Host port 5433, all interfaces | Database authentication is exposed wherever host networking/firewall permits. |
| Client → MinIO | Host ports 9000 and 9001, all interfaces | Both S3 and the administrator console are published. |
| Next.js → API | Baked rewrite origin `http://api:8000` | Fixed internal destination; separate from the public share-link origin. |
| API/worker → DB/S3 | Internal Docker network, privileged service credentials | A compromised application process has excessive backing-service authority. |
| Intended Internet edge → services | Nginx exists only as an example file | No Nginx container, TLS termination, or effective edge rate limiter was observed. |

Binding and running state were confirmed with `docker compose ps` and filtered `docker inspect`. **Public Internet reachability, host firewall rules, cloud security groups, NAT, and any external TLS gateway were not tested.** An all-interface Docker publication is not proof that a distant Internet client can reach the machine.

Application routes inspected:

- `POST /api/v1/shares`: anonymous multipart upload.
- `GET /api/v1/shares/{token}`: metadata; no listing endpoint (`GET /api/v1/shares` returned 405).
- `GET` / `HEAD /api/v1/shares/{token}/download`: token-checked attachment streaming.
- `DELETE /api/v1/shares/{token}`: additionally requires `X-Manage-Token`.
- `/`, `/s/{token}`, `/health`, `/health/live`, `/health/ready`; direct API `/docs`, `/redoc`, `/openapi.json` in development mode.
- Next.js framework routes, including its upgrade handler, remain part of the exposed attack surface even when not used by product UI.

## 3. Findings

| ID | Severity | Finding | Evidence status |
| --- | --- | --- | --- |
| C01 | Critical, if deployed publicly as supplied | Published backing services use known development administrator credentials | Configuration, running bindings, credential equality, and privileges confirmed locally; remote Internet access not attempted. |
| H01 | High | Anonymous resource consumption is not bounded globally; Next.js retains upload-body copies | Missing admission/quota controls confirmed by inspection; 1 MiB replay-buffer reproduction and tiny anonymous upload confirmed. Exhaustion not attempted. |
| H02 | High | Unsupported runtimes and advisory-matching Next.js deployment | Installed versions and current vendor advisories confirmed; individual network exploits remain unverified. |
| H03 | High | No supplied HTTPS enforcement or protected origin boundary | HTTP behavior, published origin ports, and HTTP-only edge example confirmed. External deployment controls unknown. |
| M01 | Medium | Share capability URLs can enter proxy logs unredacted | Synthetic Next.js error reproduction confirmed; encoded API path confirmed live; Nginx map bypass evaluated directly, not in a running Nginx. |
| M02 | Medium | API and worker execute as root with writable filesystems | Actual `id` and container configuration confirmed; no container escape or code execution attempted. |
| L01 | Low, operational | No worker progress health signal; orphan reconciliation is disabled by default | Command, worker code, bucket lifecycle settings, and logs inspected. No current worker failure alleged. |
| L02 | Low, hardening | Recipient pages lack a strict referrer policy and browser defense headers | Headers and same-origin referrers observed; filename XSS did not execute; no cross-origin token leak established. |

### C01 — Published backing services with development administrator credentials

References: [compose.yaml:8](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L8), [compose.yaml:12](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L12), [compose.yaml:27](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L27), [compose.yaml:30](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L30), [config.py:26](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/config.py#L26), [config.py:36](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/config.py#L36).

**Evidence.** PostgreSQL and both MinIO ports are published on IPv4 and IPv6 wildcard addresses. Boolean-only comparisons of the running containers' environment confirmed that the documented default PostgreSQL password and MinIO root credentials are in use. The API's S3 credentials equal MinIO's root credentials. A read-only query confirmed the API's DB role has `rolsuper = true`. Credentials themselves were not printed or copied into this report.

**Attack.** On a machine exposing these ports to the Internet, anyone can use credentials already disclosed by the repository to authenticate to storage or PostgreSQL. This bypasses share/manage-token authorization altogether and permits disclosure or destruction of all hosted data, rather than one attacker's share. A private bucket does not protect against authenticated root access.

**Safe reproduction.** Inspect `docker port sendrift-postgres` and `docker port sendrift-minio`; compare configured credentials to the checked-in defaults in memory, outputting only equality booleans. Using the existing configured connection, execute `SELECT rolsuper FROM pg_roles WHERE rolname = current_user`. These checks succeeded. No destructive administrator operation was performed.

**Recommendation.** Do not publish PostgreSQL, S3, or the MinIO console to untrusted networks. Replace and rotate development credentials, make production reject defaults, separate migration/admin authority from runtime CRUD, and use bucket-scoped application credentials. Verify the real firewall from outside before launch. Critical severity is conditional on public deployment with this configuration, not a claim of an existing Internet breach.

### H01 — Anonymous capacity abuse and retained proxy bodies

References: [upload_limits.py:14](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/upload_limits.py#L14), [shares.py:46](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/api/v1/shares.py#L46), [database.py:15](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/database.py#L15), [share_service.py:104](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/services/share_service.py#L104), [share_repository.py:86](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/repositories/share_repository.py#L86), [nginx.conf.example:15](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/infra/proxy/nginx.conf.example#L15), [next.config.mjs:11](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/next.config.mjs#L11), [compose.yaml:41](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L41).

**Evidence.** There is a per-file 100 MiB guard and a 105 MiB received-body guard. There is no aggregate stored-byte quota, upload/download rate limit, bandwidth budget, admission/concurrency guard, or free-space cutoff in the application or Nginx example. Running containers have Docker memory limit `0` and no PID limit. Compose has no CPU/memory/storage resource limits. A foreign-origin multipart upload returned 201; absence of CORS permission prevented browser response access, not server-side allocation.

The installed Next.js rewrite path calls `cloneBodyStream()`. Its implementation pushes every chunk into a second unread `PassThrough` for replay without honoring backpressure on that retained copy. An isolated **1 MiB** test consumed the proxy stream, finalized it, and observed **1,048,576 bytes still buffered** in the replacement body. Thus the FastAPI/S3 streaming implementation does not establish constant-memory behavior across the whole web path. Runtime evidence is in installed Next.js `dist/server/body-streams.js` and `dist/server/lib/router-server.js`; the checked-in version is [package-lock.json:4153](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L4153).

FastAPI parses/spools multipart before the route validates exactly one file. The body budget still bounds each request, but multiple small file parts can occupy parser memory before rejection. Valid files over the spool threshold use temporary disk before DB/storage processing. Uploads hold DB locks during S3 transfer; slow downloads can also retain a request-scoped DB session after the final authorization query. Pool limits protect DB connection count, not the number of incoming requests or bytes already spooled/buffered. Filename metadata has no length bound in [models/share.py:53](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/models/share.py#L53); basename sanitization is not a metadata-capacity limit.

**Attack.** Repeated valid uploads retained for seven days consume disk, DB records, and storage; repeated downloads consume egress. Concurrent or slow requests occupy proxy buffers, multipart spools, thread workers, DB sessions, and sockets. No token guessing is necessary: an attacker creates and reuses their own share. Filenames are also unbounded `Text` metadata, so a tiny file need not imply a small metadata response or DB row. This metadata path was inspected, not stress-tested.

**Safe reproduction.** Submit one small anonymous multipart request with `Origin: https://attacker.invalid`: observed 201 without an allow-origin response header. The 1 MiB isolated Next.js test demonstrated retained buffering. Existing body-limit regression tests passed for absent/incorrect declared lengths. No traffic flood, disk-fill test, or concurrency exhaustion was run.

**Recommendation.** Before public exposure, enforce trusted-edge rate and connection limits, bounded concurrent work and upload duration, total storage/egress budgets, temporary-disk budgets, metadata length limits, and resource/container limits. Ensure direct API/web ports cannot bypass edge policy. Account for Next.js buffering when measuring memory; do not describe the entire path as bounded to the S3 chunk size. Add capacity/abuse alerts and a documented complaints/takedown and malware-risk policy. Use controls shared across workers, not a process-local counter.

### H02 — Unsupported runtimes and known dependency advisories

References: [package.json:14](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package.json#L14), [package-lock.json:4153](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L4153), [next.config.mjs:11](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/next.config.mjs#L11), [Dockerfile:1](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/Dockerfile#L1), [Dockerfile:16](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/Dockerfile#L16).

**Evidence.** Docker is running Next.js **14.2.35**, React **18.3.1**, and Python **3.10.22**. The [Next.js support policy](https://nextjs.org/support-policy) lists 14.x as unsupported. The [Python release-status table](https://devguide.python.org/versions/) lists Python 3.10 as end-of-life on **2026-10-01**.

Production-only `npm audit --json --package-lock-only --omit=dev` flagged two package nodes: `next` (critical catalog severity) and its nested `postcss` (high). Full-lockfile audit flagged 12 package nodes: 1 critical, 9 high, 2 moderate. These totals are **not counts of independently exploitable flaws in Sendrift**.

Relevant Next.js cases include the vendor's [WebSocket upgrade SSRF advisory](https://github.com/vercel/next.js/security/advisories/GHSA-c4j6-fc7j-m34r) and [rewrite request-smuggling advisory](https://github.com/vercel/next.js/security/advisories/GHSA-ggv3-7p47-pfv8). Sendrift uses the self-hosted Node server, exposes it directly, and uses external rewrites. These warrant remediation even though exploitation was not established here. Two bounded absolute-target WebSocket probes to local API/MinIO **health endpoints only** returned an error, not a successful internal response. No smuggled second request, metadata endpoint, exploit payload, or RCE payload was sent.

**Recommendation.** Move to supported, currently patched runtime releases after reviewing all applicable advisories and testing App Router, rewrites, downloads, and deployment behavior. Do not use `npm audit fix --force` blindly. Old advisory patch versions are not automatically safe as of this audit date. Until patched, block unnecessary upgrades at the trusted edge and make the origin inaccessible directly; this is mitigation, not a substitute for a supported framework. See section 5 for applicability exclusions and Python inventory.

### H03 — Plain HTTP and no enforced public ingress boundary

References: [nginx.conf.example:51](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/infra/proxy/nginx.conf.example#L51), [compose.yaml:67](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L67), [compose.yaml:123](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L123), [config.py:21](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/config.py#L21), [config.py:50](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/config.py#L50).

**Evidence.** The example listens only on port 80, with no TLS listener, HTTPS redirect, or HSTS. Compose deploys no Nginx service and publishes web/API directly. The running public-link origin uses HTTP. `PUBLIC_WEB_URL` is validated as an HTTP(S) origin, but production mode does not require HTTPS or reject development credentials. Local HTTP upload, metadata, and download all succeeded.

**Attack.** If this topology is published without another verified TLS edge, on-path observers can obtain file contents, share capabilities, and the one-time management secret. If only Nginx is hardened while the origin ports stay public, attackers bypass its body/rate/upgrade restrictions. This is not a finding against a separately secured, uninspected external gateway.

**Recommendation.** Establish and verify the actual HTTPS ingress, canonical public HTTPS origin, redirect/HSTS policy, and a private/restricted origin network before exposing anonymous users. Restrict backing services and reject insecure production configuration. Do not assume the comment that Nginx “hides storage” overrides Docker's independent published ports.

### M01 — Incomplete token redaction across proxy layers

References: [next.config.mjs:12](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/next.config.mjs#L12), [nginx.conf.example:21](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/infra/proxy/nginx.conf.example#L21), [nginx.conf.example:28](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/infra/proxy/nginx.conf.example#L28), [nginx.conf.example:36](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/infra/proxy/nginx.conf.example#L36), [logging.py:9](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/core/logging.py#L9).

**Evidence A: Next.js errors.** Installed Next.js `dist/server/lib/router-utils/proxy-request.js` logs `Failed to proxy ${target}` through `console.error`. The API's redacting formatter does not sanitize Node logs. In an isolated process, the real installed proxy implementation was pointed at a closed loopback port with the synthetic path `/api/v1/shares/AUDIT_SYNTHETIC_TOKEN_NOT_A_REAL_SECRET/download`. It returned 500 and its captured error message contained the entire marker. No real capability was used in this error test and no production service was stopped.

**Evidence B: Nginx access-map bypass.** Its map matches literal prefixes in raw `$request_uri`; unmatched input is logged verbatim. A real, authorized metadata request to `/api/v1/%73hares/<fixture-token>` returned 200 from FastAPI. Evaluating the example's map patterns against that raw URI took the default branch, which would retain the full capability. The same normalization mismatch must be considered for the referer map. Nginx itself was not running; this establishes the deterministic mapping flaw and accepted backend path, not an observed deployed Nginx log entry. Ordinary error logs are also outside these access-log maps; their behavior requires deployment testing.

**Impact.** Anyone with access to affected log streams can replay a still-valid share token. This does not reveal the independent management secret, and unauthenticated Internet access to the logs was not found. That prerequisite limits severity to Medium here.

**Recommendation.** Sanitize both access and failure logs at every component, normalize paths before matching sensitive routes, cover encoded-prefix variants and referrers, and avoid recording raw proxy targets for capability routes. Re-test upstream failures without putting actual user capabilities in diagnostic output. A correct FastAPI happy-path redaction test is insufficient.

### M02 — Root application processes and unrestricted writable filesystems

References: [API Dockerfile:16](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/Dockerfile#L16), [API Dockerfile:22](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/Dockerfile#L22), [API Dockerfile:29](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/Dockerfile#L29), [compose.yaml:80](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L80).

**Evidence.** `docker exec ... id` returned `uid=0(root)` for both API and worker. Their Dockerfile has no non-root `USER`. Running application containers have writable root filesystems and no explicit capability-drop/security-option restrictions in Compose. Containers are **not** privileged, and no Docker socket mount was observed. Next.js correctly runs as `nextjs`; MinIO uses UID 65532.

**Impact.** This amplifies a future code-execution or filesystem-write vulnerability. It is not itself proof of RCE, host escape, or unauthorized file access. Combined with C01's service authority, an application compromise has little privilege separation from the whole dataset.

**Recommendation.** Run API/worker under a dedicated unprivileged user, restrict writable temporary paths and their capacity, reduce Linux capabilities, use appropriate no-new-privileges/read-only settings, and test multipart spooling under those restrictions. Limit DB/S3 authority separately, retaining fail-closed bucket privacy verification.

### L01 — Cleanup progress and orphan-retention monitoring gaps

References: [compose.yaml:85](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L85), [compose.yaml:87](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L87), [cleanup.py:23](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/worker/cleanup.py#L23), [cleanup.py:39](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/worker/cleanup.py#L39), [cleanup_service.py:122](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/services/cleanup_service.py#L122).

The API-specific HTTP healthcheck is appropriately disabled for the worker; it must not be restored as a false process-health test. However, there is no replacement progress signal or alert configuration. The default command omits `--reconcile`; orphan scanning is therefore not performed. The current bucket has no lifecycle configuration and has versioning disabled.

The actual worker was healthy in the operational sense: logs recorded successful cycles and deletion of the two audit fixtures, with zero storage/DB errors. Known failed/uploading/deleting rows have cleanup paths. This finding concerns future stalled cleanup and objects lacking recoverable metadata, for example after DB restore; an ordinary API operation reliably producing permanent untracked objects was **not** demonstrated.

Recommendation: monitor successful-cycle age, pending cleanup volume and errors; define and test orphan/multipart retention and recovery procedures. Any scheduled reconciliation must retain the existing age grace, DB existence recheck, and active-upload protection. API authorization must remain independent of worker availability.

### L02 — Missing browser defense headers and page-wide referrer protection

References: [layout.tsx:12](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/app/layout.tsx#L12), [next.config.mjs:5](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/next.config.mjs#L5), [recipient page:55](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/app/s/[token]/page.tsx#L55), [recipient page:112](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/app/s/[token]/page.tsx#L112).

Recipient HTML had no `Referrer-Policy`, CSP, or framing restriction. In a fresh Chrome context, 11 same-origin requests carried the full synthetic share-page URL in their `Referer`. No external requests or cross-origin token disclosure were observed. API metadata requests explicitly use `no-referrer`, downloads use `no-referrer` and `no-store`, and the recipient HTML was `private, no-cache, no-store`.

A live hostile filename was rendered literally: no injected `<img onerror>` element, dialog, or script execution. Missing CSP is therefore **not** reported as a demonstrated XSS exploit. This is a low-severity privacy/defense-in-depth weakness that increases reliance on every same-origin logging surface remaining safe.

Recommendation: set a deliberate page-wide no-referrer policy for capability pages, appropriate framing protections and CSP compatible with the patched Next.js version, and explicit no-store on sensitive JSON including the creation response. Test these policies rather than adding unsafe nonces or weakening them to make a build pass.

## 4. Lifecycle/concurrency assessment and unverified risks

- **Upload ownership:** the DB uploading row is committed before S3 activity, then locked during storage transfer. Cleanup uses `FOR UPDATE SKIP LOCKED`. A caller canceled during a running SDK thread waits for that thread before releasing its lock/spool. Inspected at [share_service.py:104](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/services/share_service.py#L104), [share_repository.py:86](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/repositories/share_repository.py#L86), [s3.py:271](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/storage/s3.py#L271).
- **Worker ownership:** transition to deleting is committed durably; ownership is reacquired before S3 deletion and retained through DB removal. S3 failure leaves retryable state, and a crash after S3 removal leaves a DB row eligible for retry. See [cleanup_service.py:68](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/services/cleanup_service.py#L68) and [share_repository.py:145](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/repositories/share_repository.py#L145). No new cross-worker data-loss bug was established by inspection or the unit suite.
- **Stale uploads:** candidates use `updated_at`, with configurable one-hour grace. Active uploads holding their row lock are skipped; initial multipart body reception is not yet a storage-backed share and instead consumes spool resources, covered by H01.
- **Multipart cleanup:** deletion enumerates/aborts unfinished multipart uploads for the **exact** object key before deleting the object. Orphan listing also handles multipart timestamps and paginated completed objects. See [s3.py:329](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/storage/s3.py#L329), [s3.py:380](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/storage/s3.py#L380).
- **Orphan safety:** age grace, paginated bounded listing, a batch DB lookup, and a second per-key DB lookup protect recent/tracked objects. Safety assumes the server-generated keys are never reused, consistent with the upload code. No live orphan reconciliation was invoked during this audit.
- **Download/revocation race:** status/UTC expiry are checked before opening S3 and again after refreshing DB state. The counter update is conditional on readiness/expiry. A transfer already authorized may continue after expiry or revocation; this is not a new-request authorization bypass. Exact behavior of an in-flight MinIO read during concurrent physical deletion was not exercised.
- **Forced termination and ambiguous commit:** no process was killed and no DB/S3 failure was injected into the running stack. Before-commit failure tests do not prove recovery after a server commits but its acknowledgement is lost. Finalization compensation can leave a ready row without its object after an uncertain commit; no share URL is returned on that failure path, so this is not established as loss of an already acknowledged share. Further isolated failure testing is warranted.
- **Versioned buckets:** current versioning is disabled. Enabling S3 versioning later changes physical-erasure semantics; ordinary DeleteObject can retain old versions. Such a deployment needs an explicit retention/deletion policy and was not validated here.
- **Proxy/IP trust:** no authorization or limiter currently trusts `X-Forwarded-For`. The Nginx example appends supplied forwarding headers. A future IP-based control must establish the actual trusted proxy chain and use the edge-observed client address; an existing IP-limit bypass is not alleged because no such limiter exists.
- **Alternate Compose file:** [infra/compose.yaml:80](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/infra/compose.yaml#L80) still has the old worker probe inheritance and omits the explicit public-origin/build-argument wiring in the root configuration. It was inspected but not launched. It should not be treated as a separately validated production deployment.

## 5. Dependency review

No packages were installed, upgraded, or fixed. Package names/versions, not application data or secrets, were submitted to public advisory services.

### npm

| Scope | Actual result | Interpretation |
| --- | --- | --- |
| Full lockfile | 12 flagged package nodes: 1 critical, 9 high, 2 moderate | Includes transitive and development dependency chains, not 12 independent remote exploits. |
| Production-only | `next` critical; nested `postcss` high | Deployment blocker for supported/patchable runtime review; individual feature applicability still matters. |
| Next.js 14.2.35 | Docker version matches [lockfile:4153](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L4153) | Unsupported, self-hosted with external rewrites. |
| Nested PostCSS 8.4.31 | [lockfile:4203](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L4203) | Advisories concern CSS processing/source maps; no route parsing uploaded files as CSS was found. Not a demonstrated remote file read in Sendrift. |
| glob 10.3.10 | [lockfile:3033](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L3033), [CLI advisory](https://github.com/advisories/GHSA-5j98-mcp5-4vw2) | Developer-tool command injection requires the affected CLI mode and attacker-controlled matches. No web-request invocation found. |
| braces 3.0.3 / selector parser 6.1.4 | [lockfile:1596](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L1596), [lockfile:4762](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/package-lock.json#L4762) | Build/tooling parser DoS matches; untrusted uploaded files are not build inputs. |

Critical Next.js advisories were **not blindly promoted to confirmed RCE**:

- [Windows-hosted RCE advisory](https://github.com/vercel/next.js/security/advisories/GHSA-p293-qw3h-jr36): version match, but production container is Linux. Windows hosting was not exploited or validated as safe.
- [AVIF optimizer RCE advisory](https://github.com/vercel/next.js/security/advisories/GHSA-2xp9-vwfh-vxw4): requires the affected `sharp`/libheif processing path. `sharp` is not installed in the observed web image. No crafted image was sent.
- [React Server Components RCE advisory](https://github.com/vercel/next.js/security/advisories/GHSA-9qr9-h5gf-34mp): its stable-version scope is not this React 18 / stable Next 14.2 deployment. No React2Shell compromise is claimed.
- Middleware authorization-bypass, nonce/`beforeInteractive` XSS and shared-cache poisoning advisories need features/configurations not established here: no authorization middleware, untrusted inline script construction, nonce CSP, or deployed shared cache was found.
- Rewrite smuggling and self-hosted upgrade handling deserve particular attention because those code paths exist. Version/advisory applicability was established; exploit success was not.

### Python and images

OSV `querybatch` checked **43 installed distributions inside the API image**. Matches were returned for **pip 23.0.1** and **setuptools 79.0.1**; no match was returned for the other 41 distributions. Representative records are [pip CVE-2025-8869](https://osv.dev/vulnerability/GHSA-4xh5-x5gv-qwph) and [setuptools CVE-2026-59890](https://osv.dev/vulnerability/GHSA-h35f-9h28-mq5c). These are package/build workflows; the latter concerns macOS filesystem normalization. They are not demonstrated HTTP upload attacks on this Linux image. OSV included additional pip records/aliases; absence of an OSV match is not a clean-bill-of-health certification.

Observed application versions included FastAPI 0.143.0, Starlette 1.7.0, python-multipart 0.0.32, Uvicorn 0.54.0, SQLAlchemy 2.0.54, boto3/botocore 1.43.111, and h11 0.16.0. Runtime inventory also showed Node 22.23.3, PostgreSQL 16.15, and MinIO `RELEASE.2026-09-22T19-25-18Z`. Python 3.10.22 is end-of-life as noted in H02.

Reproducibility gaps: [requirements.txt:1](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/requirements.txt#L1) has version ranges and no hashed resolved Python lock; [web Dockerfile:6](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/web/Dockerfile#L6) falls back from `npm ci` to `npm install`; base images and MinIO use mutable tags ([compose.yaml:23](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/compose.yaml#L23)). A future rebuild need not reproduce this inventory. Pin/review resolved artifacts and fail on lockfile inconsistency; do not blindly upgrade every flagged development package. OS-level, Go-binary, and full image vulnerability scans were not available and were not installed.

## 6. Security controls implemented correctly

1. **Secrets:** separate `secrets.token_urlsafe(32)` values; SHA-256 hashes only in DB; management comparison uses `secrets.compare_digest`. See [share_service.py:51](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/services/share_service.py#L51), [share_service.py:292](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/services/share_service.py#L292). Live DB hash checks succeeded. Metadata did not disclose the management secret, and that secret did not act as a share token.
2. **Authorization:** missing/unknown share capabilities and wrong/missing management secrets did not grant access. Revocation returned 202; subsequent metadata/download returned 404; repeat revocation returned 202 before physical cleanup. Expiration uses server UTC, independent of worker success. No cross-user enumeration route was found.
3. **Storage privacy:** startup fails closed when ACL/policy privacy cannot be verified ([s3.py:142](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/storage/s3.py#L142)); existing bucket privacy is checked, not silently rewritten. Actual bucket: no public ACL group, no bucket policy, unsigned list/read both 403. Privacy tests cover public policies and startup failures without modifying the live policy.
4. **File handling:** random object keys independent of filenames; basenames for display; no file execution/preview pipeline. Downloads force `application/octet-stream`, attachment disposition, `nosniff`, `no-store`, and `no-referrer`. A tiny HTML payload downloaded byte-for-byte and did not execute as page content.
5. **Size checks:** received-body counting does not trust Content-Length; file bytes are checked again in the S3 adapter. The serial classic multipart transfer uses 8 MiB chunks rather than the SDK's default in-memory upload queue ([s3.py:219](https://github.com/alastrm/Sendrift/blob/3f00603f0a7e6965d3e45a167f451db31fe3c72e/apps/api/app/storage/s3.py#L219)). These are per-operation controls, not H01's missing aggregate protection.
6. **URL construction:** public origins come from validated `PUBLIC_WEB_URL`; hostile Host/forwarded headers did not alter the returned link. Rewrite destinations use a server-only build setting, not attacker-supplied URL hosts. No application-level open redirect was found.
7. **Browser handling:** React renders filename text; live hostile filename test produced no DOM injection or script execution. Management secrets are held in component state, not localStorage/sessionStorage or public-link query parameters.
8. **CORS/CSRF:** configured origins are explicit. A hostile-origin DELETE preflight returned 400. There are no ambient authentication cookies that would automatically authorize deletion; a management header and valid share capability remain necessary. Anonymous cross-origin upload acceptance is an abuse/capacity concern, not proof of account-CSRF or CORS disclosure.
9. **Errors and queries:** SQLAlchemy parameterized expressions are used, and API error handlers generally expose neutral errors while logging exception types rather than internal DB/S3 messages. API path redaction works for ordinary capability paths. M01 concerns separate proxy layers.
10. **Repository hygiene:** a fresh scan of 88 tracked text files found no private-key, GitHub-token, AWS access-ID, or JWT-pattern matches; no real `.env` is tracked. The example/default development credentials are public by design and remain unsafe when actually used as in C01. This was a pattern scan, not a full secret/history forensics review.

## 7. Tests actually performed in this audit

| Check | Result |
| --- | --- |
| Read AGENTS, ARCHITECTURE, CONTEXT; map code/routes/config/runtime | Completed at the commit above. |
| `python -m ruff check --no-cache .` | Passed. |
| `python -m pytest -q -p no:cacheprovider` | **97 passed, 48 skipped**, 3.04 seconds. Opt-in integration modules remained disabled. |
| `node --test tests/proxy-config.test.mjs` | Passed: three origin scenarios plus parent test, 4 Node test results. |
| `npm run typecheck -- --incremental false` | Passed. |
| `npm run lint -- --no-cache` | Passed, no warnings/errors. |
| Full/production-only npm audit | Completed with findings; each exited 1 because advisories were present. Counts in section 5. No `--fix` was used. |
| OSV query of running Python inventory | Completed for 43 distributions; two package names matched. |
| Container exposure, limits, privilege and default-credential equality | Confirmed; output limited to configuration fields and booleans, never secrets. |
| Actual local upload/metadata/download/revocation | Tiny fixtures only; correct byte content, GET attachment headers, hash-only DB fields, wrong-secret denial, and post-revocation denial confirmed. No separate live HEAD request was made; HEAD behavior was covered by the existing unit suite. |
| Invalid TTL and duplicate multipart file requests | Both 400; rejected before successful share creation. |
| Foreign-origin multipart POST / DELETE preflight | POST 201 without allow-origin header; preflight 400. No authenticated CSRF bypass. |
| Actual unsigned MinIO GET/list and privacy metadata | Both unsigned operations 403; no public ACL groups or bucket policy. Bucket versioning disabled; no lifecycle configuration. |
| Encoded route prefix | `/api/v1/%73hares/<own-token>` returned 200; Nginx raw-URI map default-branch leakage confirmed by direct pattern evaluation. |
| Isolated actual Next.js proxy failure | 500 and complete synthetic capability marker in captured error message. No real token logged or upstream stopped. |
| Isolated actual Next.js clone buffering | A 1 MiB consumed request retained a 1 MiB replay buffer. No OOM/disk-fill attempt. |
| Two local WebSocket absolute-target health probes | Error response; **SSRF success not established**. No other destinations tested. |
| Chrome hostile filename/referrer test | Literal text, 0 dialogs, no script execution, 0 external requests; 11 same-origin requests carried the share URL as referrer. |
| Ordinary worker logs following fixture revocation | Two one-row/one-object deletion cycles followed the audit revocations; zero reported storage/DB errors. A final read-only query found zero remaining audit-fixture rows. No manual cleanup cycle invoked. |
| Repository status before writing report | Clean; no source/infrastructure changes made by the audit. |

The live download response headers were inspected on GET. Unit tests additionally covered HEAD. Earlier chat test results, including prior 100 MiB Docker smoke tests, are **not counted as tests executed during this audit**.

### Reproduction notes

Use a private local stack only, one synthetic file at a time. Parse creation responses in memory; do not paste bearer secrets into shell arguments, saved command history, access logs, or this report.

- C01: filtered `docker inspect` / `docker port`, boolean comparisons with repository defaults, and the read-only DB-role query above. Do not attempt this against a third-party server.
- H01: inspect the absence of `limit_req`, `limit_conn`, quotas/admission controls and Compose resource limits; compare Docker memory/PID limits. For the proxy buffer, import installed `getCloneableBody`, feed `Readable.from([Buffer.alloc(1024 * 1024)])`, consume `cloneBodyStream()`, call `finalize()`, then inspect `readableLength` of the replaced source. Expected observed value: 1,048,576. Do not scale this into a stress test.
- M01, encoded path: upload a tiny local fixture, request its metadata with literal `shares` changed to `%73hares`, and compare the raw path to the two `map $request_uri` patterns at Nginx lines 22–24. It still identifies the fixture in FastAPI but misses both raw-prefix patterns. Revoke the fixture afterward.
- M01, failure log: invoke the installed `proxyRequest` in an isolated loopback test server, using a closed target port and **only** `AUDIT_SYNTHETIC_TOKEN_NOT_A_REAL_SECRET` as the path marker. Capture `console.error` in memory and report a boolean marker match, not actual user URLs. Do not stop the real API to trigger this check.
- Access controls: use the fixture's actual token only in an HTTP client's memory; try missing/wrong management headers, then the issued header. Verify metadata/download become 404 and revoke again. Never reuse an unrelated share.

## 8. Remediation order and release gates

**P0 — required before anonymous Internet exposure**

1. Close backing/admin and bypass origin ports; replace development credentials and runtime administrator roles. Verify actual external reachability and firewall policy (C01).
2. Deploy/test a trusted HTTPS edge and canonical HTTPS public URL; ensure direct origin access cannot bypass it (H03).
3. Implement/test aggregate resource and abuse controls, including upload concurrency/spooling, quotas, egress limits, free-space handling, and alerts. Establish complaints/takedown and malware-risk procedures (H01; required by AGENTS).
4. Plan and test supported patched Next.js and Python runtimes. Review applicable advisories individually; constrain unnecessary upgrades/egress while remediation is pending (H02).
5. Correct capability redaction for encoded paths and proxy failure logs before introducing real user traffic (M01).

**P1 — complete in the hardened deployment and verify operationally**

6. Run API/worker without root and with bounded writable storage, minimal capabilities, and scoped DB/S3 permissions (M02).
7. Monitor cleanup progress, stale/failed/deleting populations, orphan/multipart retention, and capacity. Validate recovery from DB restore and worker failure without unsafe reconciliation (L01).
8. Set/test recipient privacy and browser security headers, sensitive JSON cache policy, and a production-safe diagnostics exposure policy (L02).
9. Make builds reproducible: reviewed resolved Python dependencies/hashes, no npm lockfile fallback, reviewed image versions/digests, and recurring image/package advisory checks.

**Release verification still needed:** isolated multi-process PostgreSQL locking tests, real interruption/forced-termination and uncertain-commit tests, in-flight download/cleanup tests, and actual Nginx/TLS/log-redaction tests including encoded routes and upstream failures. Recheck the public topology and advisories after remediation. Passing the current unit suite alone is not the release gate.

## 9. Audit limitations

- Point-in-time source/configuration review and bounded local tests, not exhaustive penetration testing or certification.
- No host firewall/cloud account/TLS terminator/public-domain assessment. Nginx was not deployed, launched, or modified; its example configuration was reviewed and its raw-path matching evaluated separately.
- No dependency installation, version change, image rebuild, service restart/kill, bucket policy change, schema change, stress/load test, disk fill, RCE payload, live request-smuggling attempt, or cloud-metadata probe.
- No full container OS/Go binary scan, firmware/host assessment, Git history secret audit, backup/restore exercise, or external incident-response verification.
- npm/OSV/vendor advisory results are dated observations, with feature/platform prerequisites. OSV returning no package match does not prove absence of vulnerabilities.
- Forty-eight opt-in tests were skipped. The separate live checks described here do not substitute for running those tests in a disposable PostgreSQL/MinIO environment. Existing real tests can create/drop schemas or buckets or manipulate cleanup state and were deliberately not enabled against the shared stack.
- Live audit fixtures were tiny. The 100 MiB and 100 MiB+1 network boundaries, maximum multipart overhead, sustained memory growth and high concurrency were not re-exercised; only bounded unit/isolated tests and code inspection support those aspects here.
- No promise of antivirus scanning, end-to-end encryption, immediate physical deletion after revocation, or retroactive cancellation of an already authorized transfer is justified by this audit.

**Decision: keep Sendrift restricted to local/private testing until the P0 gates are closed and the hardened public deployment is re-audited.**
