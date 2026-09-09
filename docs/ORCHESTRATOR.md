# Orchestrator behavior for adopters

This document describes the public contract of the Marsh orchestrator: how it
learns about demand, how a runner cycle runs, and how optional features reduce
pickup latency or help debug failed jobs. It uses only synthetic examples.

Keep activated profiles, webhook public URLs, reverse-proxy configs, and
HMAC secrets in your private operations source.

## Control loop

GitHub Actions remains the scheduler. Marsh does not replace GitHub's queue.

1. The orchestrator **reconciles** demand on a timer (`[poller].interval_secs`).
2. For each size class it mints JIT runner configs and creates Daytona sandboxes
   until supply matches queued demand (and any explicit warm floor).
3. Each sandbox runs the GitHub Actions runner for **one job**, then is deleted.
4. An optional **webhook** listener can wake the reconciler as soon as GitHub
   queues a matching job, without waiting for the next poll interval.

The poller is always the source of truth. Webhooks are a best-effort fast path
(GitHub may delay or drop delivery). If the webhook is down, Marsh still scales
from the poller alone.

## Reconcile cadence

Every cycle state change (spawned, online, busy, gone) wakes the main loop so
runtime status publishes promptly. A wake does not by itself trigger a GitHub
scan: scans run at most once per `[poller].min_tick_secs` (default `5`, never
more than `interval_secs`) and at least once per `interval_secs`. Without that
floor a burst of jobs turned every cycle transition into a full scan, exhausted
the GitHub App's primary rate limit, and froze spawning for the whole cooldown.

List requests send `If-None-Match` with the last `ETag` GitHub returned for
that URL. An unchanged page comes back `304` and is not charged against the
primary rate limit, so a quiet repository costs almost no budget per tick.

## Provider capacity and cleanup-pending cycles

Daytona enforces an organization-wide compute quota shared by every fleet on
the account (for example `Total CPU limit exceeded. Maximum allowed: 250.`).
When a create is refused for capacity, the orchestrator:

1. logs `sandbox create refused for provider capacity` for that cycle;
2. pauses new spawns for `PROVIDER_CAPACITY_BACKOFF_SECS` (30s) across all
   size classes, logging `deferring +N; provider capacity backoff` per tick;
3. emits cycle telemetry with `termination_reason = provider_capacity` and
   `cleanup_status = create_failed` (a provider-confirmed zero allocation).

Every sandbox is labeled `cycle=<cycle id>`, and JIT runners are named
`marsh-<first 12 chars of the cycle id>`. After any failed create the
orchestrator asks the provider whether a sandbox with that label exists: absent
means the slot is released immediately; present means the sandbox is adopted
and torn down normally. Only when the provider cannot answer does the cycle
stay `CLEANUP_PENDING`, and it still counts against the class `max` while it
does. Each tick then retries every pending cycle (delete by id, deregister the
runner, or re-check the label) and releases the ones the provider confirms
clean; a doubt with no id to act on is settled by the next successful orphan
sweep that ran after the cycle's grace window. The log line
`cleanup pending: released N cycle(s)` records each recovery.

Sum the `cpu` of every size class `max` across all fleets that share one
Daytona organization; when that exceeds the quota, the backoff is what keeps a
burst from turning into a storm of refused creates, but only a higher quota
adds capacity.

## Adaptive cycle polling

After a runner process starts inside a sandbox, the cycle thread polls for job
pickup, natural exit, and deadlines. Defaults favor fast pickup right after
online, then settle:

| Setting | Default | Role |
| --- | --- | --- |
| `fast_idle_poll_secs` | `3` | Poll interval while idle and newly online |
| `fast_idle_window_secs` | `90` | How long the fast idle window lasts |
| `idle_poll_secs` | `15` | Poll interval after the fast window |
| `busy_poll_secs` | `10` | Poll interval while the runner is busy |

These live under `[lifecycle]` in the fleet profile. Values must be positive
integers (except where `0` is documented elsewhere).

## Optional GitHub webhook (fast path)

### Profile shape

```toml
[webhook]
listen = "127.0.0.1:8787"
hmac_env = "MARSH_WEBHOOK_HMAC"
```

- `listen` must be loopback or `0.0.0.0` (`host:port`). Prefer loopback behind
  your own reverse proxy.
- `hmac_env` is the **name** of a host environment variable that holds the
  GitHub webhook secret. Never put the secret value in the profile or image.

### Host environment

```bash
export MARSH_WEBHOOK_HMAC="<github-app-webhook-secret>"
```

Use the same secret GitHub shows when you configure the App webhook.

### GitHub App settings

1. Set the App webhook URL to your public endpoint that terminates TLS and
   proxies to the orchestrator listener (for example `https://marsh.example/github`).
2. Subscribe to the **Workflow jobs** event (`workflow_job`).
3. Deliver the same App that installs on the orgs/repos this fleet serves.

Accepted paths on the listener: `/`, `/github`, `/webhook`.  
Health check: `GET /healthz` or `GET /` returns `ok`.

### Matching rules

On `workflow_job` with `action=queued`, Marsh wakes the poller only when the
job's labels are a **superset** of at least one configured size-class label set
(for example `self-hosted`, `daytona`, `marsh`). Unrelated `runs-on` values are
ignored. `ping` events return `200` for setup checks. Invalid signatures return
`401`.

### Security notes

- Signatures use `X-Hub-Signature-256` with constant-time compare.
- Request bodies larger than 1 MiB are rejected.
- The secret must never appear in logs, profiles, or the runner image.
- Do not expose the listener on the public internet without TLS termination and
  network policy you control.

## Hold on failure (debug)

```toml
[lifecycle]
hold_on_failure_secs = 600
```

When a cycle ends as `failed` or with a nonzero runner exit code, Marsh keeps
the Daytona sandbox for this many seconds before delete. Use that window to
attach with Daytona SSH (or your provider's console) and inspect the workspace.
`0` disables the hold (default). A fleet stop/drain still interrupts the wait.

Do not enable long holds on untrusted or public workflows without a reviewed
trust boundary.

## Cycle telemetry

Each completed cycle emits one journal line:

```text
cycle_telemetry {"event":"runner_cycle_complete","schema_version":1,...}
```

Useful stage fields (seconds, controller-observed wall time):

| Field | Meaning |
| --- | --- |
| `jit_mint_secs` | GitHub JIT mint |
| `sandbox_create_secs` | Daytona create until ready |
| `runner_start_secs` | Start runner command after sandbox ready |
| `launch_secs` | Create-request start through runner command start |
| `idle_secs` | Runner online until first busy (or idle teardown) |
| `busy_secs` | First busy until cycle complete |
| `teardown_secs` | Delete after cycle complete |

`usage-report` (watchdog) summarizes per-snapshot counts, outcomes, duration
p95, and stage p95 when every sample in the window includes that stage.
`cleanup_status` is one of `deleted`, `delete_failed`, `create_unconfirmed`,
`create_failed` (provider confirmed nothing was allocated), or
`create_not_attempted`; the last two are known zero allocations.

Telemetry is secret-free by design: no tokens, exception bodies, or provider
error payloads.

## Related docs

- [FLEET-DEPLOYMENT.md](FLEET-DEPLOYMENT.md) — profile contract and private boundary
- [SNAPSHOT-GOVERNANCE.md](SNAPSHOT-GOVERNANCE.md) — immutable snapshots
- [SECURITY-MODEL.md](SECURITY-MODEL.md) — repository security controls
- Example profile: `config/fleets/example/runners.toml`
