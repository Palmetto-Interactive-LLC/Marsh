# ============================================================================
# GitHub Configuration
# ============================================================================

variable "github_org" {
  type        = string
  default     = "YOUR-GITHUB-ORG"
  description = "GitHub organization where the runner group and repos are located."
}

variable "github_org_lowercase" {
  type        = string
  default     = "your-github-org"
  description = "Lowercase GitHub org name (used in GHCR image path)."
}

variable "github_token" {
  type        = string
  sensitive   = true
  description = "GitHub personal access token or App token with admin:org scope (required to create/manage the runner group). Never commit; inject via environment or Secret Manager."
}

variable "runner_repos" {
  type        = list(string)
  default     = ["example-repo"]
  description = "List of GitHub repos granted access to the Marsh runner group. Repos not in this list cannot use the self-hosted runners."
}

# ============================================================================
# Daytona Configuration
# ============================================================================

variable "daytona_api_key" {
  type        = string
  sensitive   = true
  description = "Daytona API key for org access. Never commit; inject via environment or your secret manager."
}

variable "daytona_api_base" {
  type        = string
  default     = "https://app.daytona.io/api"
  description = "Base URL of the Daytona API. Defaults to the production endpoint."
}

variable "daytona_target" {
  type        = string
  default     = "us"
  description = "Daytona region target for sandboxes (US, EU, etc.)."
}

# ============================================================================
# Docker Registry (GHCR) Configuration
# ============================================================================

variable "ghcr_user" {
  type        = string
  description = "GitHub username or bot name for GHCR authentication (read:packages for Daytona pull; write:packages if also pushing). Never commit; inject via environment (TF_VAR_ghcr_user=<github-user>)."
  sensitive   = true
}

variable "ghcr_token" {
  type        = string
  description = "GitHub personal access token for GHCR (needs read:packages for Daytona pull and optionally write:packages to push images). Never commit; inject via environment (TF_VAR_ghcr_token=<pat>)."
  sensitive   = true
}

variable "ghcr_registry_name" {
  type        = string
  default     = "ghcr-marsh"
  description = "Name of the Docker registry connection in Daytona (used by snapshot registration to pull from GHCR)."
}

# ============================================================================
# Runner Image & Snapshots
# ============================================================================

variable "marsh_runner_image_tag" {
  type        = string
  default     = "v1"
  description = "Tag of the Marsh runner image (ghcr.io/.../marsh-runner:<tag>). Change this to roll a new snapshot version."
}

variable "runner_group_name" {
  type        = string
  default     = "marsh"
  description = "GitHub Actions runner group name used by Marsh ephemeral runners."
}

# ============================================================================
# Cache Volumes
# ============================================================================

variable "cache_volumes" {
  type = map(string)
  default = {
    "marsh-cache-cargo"     = "cargo"
    "marsh-cache-sccache"   = "sccache"
    "marsh-cache-go"        = "go"
    "marsh-cache-node"      = "npm"
    "marsh-cache-pip"       = "pip"
    "marsh-cache-buildx"    = "buildx"
    "marsh-cache-toolcache" = "toolcache"
  }
  description = "Map of Daytona volume names to mount paths (/cache/<mount> in runners). These are persistent FUSE volumes shared across all ephemeral runners."
}
