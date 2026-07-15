# GitHub Actions runner group for Daytona ephemeral runners.
# This group is scoped to selected repos (least privilege: unlisted repos cannot
# use self-hosted runners, preventing accidental cross-org runner usage).
# The orchestrator registers ephemeral runners into this group using JIT config.

data "github_repository" "selected" {
  for_each  = toset(var.runner_repos)
  full_name = "${var.github_org}/${each.value}"
}

resource "github_actions_runner_group" "daytona" {
  name                       = var.runner_group_name
  visibility                 = "selected"
  selected_repository_ids    = [for r in data.github_repository.selected : r.repo_id]
  allows_public_repositories = false

  # Future: restrict to specific workflows to prevent accidental runs from
  # untrusted workflow files (e.g., PRs from external contributors).
  # Example: restricted_to_workflows = ["*.github/workflows/ci.yml"]

  lifecycle {
    # Protect the group from accidental deletion.
    prevent_destroy = false
  }
}

output "runner_group_name" {
  value       = github_actions_runner_group.daytona.name
  description = "Name of the GitHub Actions runner group. Ephemeral runners register here."
}

output "runner_group_id" {
  value       = github_actions_runner_group.daytona.id
  description = "Numeric ID of the runner group (used by orchestrator JIT config)."
}

output "runner_repos" {
  value       = var.runner_repos
  description = "List of repos granted access to the Marsh runner group."
}
