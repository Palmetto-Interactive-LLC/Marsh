# Issue Tracking

GitHub Issues are the public intake and delivery record for Marsh.

## Work Item Types

| Type | GitHub form | Suggested label | Use when |
| --- | --- | --- | --- |
| Bug | `bug_report.yml` | `type:bug` | Existing behavior is reproducibly wrong. |
| Feature | `feature_request.yml` | `type:feature` | A new capability or meaningful behavior change is requested. |
| Larger effort | `epic.yml` | `type:epic` | The work needs multiple independently reviewable changes. |
| General issue | `issue.yml` | `type:issue` | The work does not yet fit another category. |

## Triage

1. Confirm the report contains enough information to reproduce or scope the work.
2. Apply one type label and any relevant priority or area labels.
3. Link prerequisites, follow-up issues, and pull requests using GitHub issue
   relationships.
4. Close the issue only after the change is reviewed, merged, and verified.

Do not put credentials, customer data, private incident details, or
vulnerability proof-of-concept material in a public issue. Follow
[`SECURITY.md`](../SECURITY.md) for private vulnerability reporting.
