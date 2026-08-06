# Issue Tracking

GitHub Issues are the public intake surface for Marsh. Palmetto Interactive
Linear is the durable delivery record.

## Linear Delivery Hierarchy

All internal Marsh work rolls up to the **Marsh** initiative in the Palmetto
Interactive Linear workspace:

1. The Marsh initiative represents this repository and its durable mission.
2. A Linear project represents a coordinated effort with multiple deliverables.
3. A Linear issue represents one independently deliverable change.

Pull requests must link the matching Linear issue or project. A public GitHub
issue may remain linked as the external intake record, but it does not replace
the Linear work item for internal implementation.

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
3. Route implementation work to a Linear issue or project under the Marsh
   initiative, then link it to the GitHub intake issue and pull request.
4. Link prerequisites and follow-up work in Linear; retain GitHub relationships
   where they help public reporters follow the outcome.
5. Close the public issue only after the change is reviewed, merged, verified,
   and reconciled in Linear.

Do not put credentials, customer data, private incident details, or
vulnerability proof-of-concept material in a public issue. Follow
[`SECURITY.md`](../SECURITY.md) for private vulnerability reporting.
