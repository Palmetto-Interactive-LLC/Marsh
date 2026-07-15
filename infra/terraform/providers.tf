terraform {
  required_version = ">= 1.6"
  required_providers {
    # 536tech/daytona: comprehensive community provider for Daytona API.
    # Covers: daytona_volume (create/read/list/delete), API-key routes.
    # Ref: https://registry.terraform.io/providers/536tech/daytona/latest
    daytona = {
      source  = "536tech/daytona"
      version = "~> 0.1"
    }

    # integrations/github: official GitHub provider for API operations.
    # Used here for: github_actions_runner_group, repository data sources.
    # Ref: https://registry.terraform.io/providers/integrations/github/latest
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }

    # null: provides null_resource for wrapping imperative scripts (snapshots, registry).
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }

    # local: for reading/writing files in orchestrator setup.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }

  }
}

# Daytona provider configuration.
# API key sourced from the environment (TF_VAR_daytona_api_key or your secret manager).
# Never hardcode or commit secrets.
provider "daytona" {
  api_key = var.daytona_api_key
  api_url = var.daytona_api_base
}

# GitHub provider configuration.
# Token sourced from environment (TF_VAR_github_token).
# Requires org runner-group admin scope (admin:org).
# Ref: https://registry.terraform.io/providers/integrations/github/latest/docs#authentication
provider "github" {
  owner = var.github_org
  token = var.github_token
}
