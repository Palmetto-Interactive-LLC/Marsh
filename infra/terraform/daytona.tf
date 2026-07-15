# Marsh platform configuration: cache volumes, registry connection, and runner snapshots.
# This file codifies the persistent infrastructure (volumes, registry) and the reproducible
# snapshot registration pipeline.

# ============================================================================
# CACHE VOLUMES
# ============================================================================
# Persistent FUSE volumes (S3-backed via mountpoint-s3) that are shared across
# all ephemeral runners. Mounted at /cache/<mount> in each runner. The orchestrator
# hooks (cache-restore.sh / cache-save.sh) manage tarball restore/save because
# mountpoint-s3 does NOT support append/rename (only whole-file writes).
# Ref: README.md section "Caching — bake the stable stuff, volume the mutable stuff"

resource "daytona_volume" "cache" {
  for_each = var.cache_volumes

  name = each.key
  # Daytona defaults to 1 GiB; adjust if cache growth exceeds this.
  # Future: make size configurable via variable if needed.

  depends_on = [
    null_resource.registry_connected
  ]

  lifecycle {
    # Protect volumes from accidental deletion (they hold persistent cache).
    prevent_destroy = true
  }
}

# ============================================================================
# DOCKER REGISTRY CONNECTION (GHCR)
# ============================================================================
# Connect GHCR to Daytona so the snapshot registry pipeline can pull the Marsh
# runner image without rebuilding on Daytona's builder.
# This wraps the idempotent infra/snapshots/setup-registry.sh script.
# Ref: setup-registry.sh for implementation details

resource "null_resource" "registry_connected" {
  provisioner "local-exec" {
    command = "${path.module}/../snapshots/setup-registry.sh"

    environment = {
      DAYTONA_API_KEY    = var.daytona_api_key
      DAYTONA_API_BASE   = var.daytona_api_base
      GHCR_USER          = var.ghcr_user
      GHCR_TOKEN         = var.ghcr_token
      GHCR_REGISTRY_NAME = var.ghcr_registry_name
    }
  }

  triggers = {
    # Reconnect if credentials or registry name change.
    ghcr_token         = var.ghcr_token
    ghcr_registry_name = var.ghcr_registry_name
  }
}

# ============================================================================
# RUNNER SNAPSHOTS (marsh-runner-default, marsh-runner-large)
# ============================================================================
# Register two sized snapshots of the Marsh runner image.
# Resources are fixed at snapshot registration (you cannot pass --cpu/--memory when creating
# from a snapshot on Daytona), so one image yields two pre-sized snapshots.
# This wraps the idempotent infra/snapshots/register-snapshot.sh script.
# Ref: register-snapshot.sh for implementation details; config/runners.toml for sizes.

resource "null_resource" "snapshots_registered" {
  provisioner "local-exec" {
    command = "${path.module}/../snapshots/register-snapshot.sh ${var.marsh_runner_image_tag}"

    environment = {
      DAYTONA_API_KEY  = var.daytona_api_key
      GHCR_USER        = var.ghcr_user
      GHCR_TOKEN       = var.ghcr_token
      GHCR_IMAGE_OWNER = var.github_org_lowercase
      GHCR_IMAGE_NAME  = "marsh-runner"
    }
  }

  # Trigger re-registration if the image tag (version) changes.
  triggers = {
    marsh_runner_image_tag = var.marsh_runner_image_tag
  }

  depends_on = [
    null_resource.registry_connected
  ]

  lifecycle {
    # Allow re-registration on demand; snapshots should be immutable but
    # rebuilding them is the intended way to roll a new version.
    create_before_destroy = true
  }
}

# ============================================================================
# OUTPUTS
# ============================================================================

output "cache_volumes" {
  value       = { for k, v in daytona_volume.cache : k => v.id }
  description = "Cache volume IDs; the orchestrator mounts these into runner sandboxes at /cache/<mount>."
}

output "registry_name" {
  value       = var.ghcr_registry_name
  description = "Name of the Docker registry connection in Daytona."
}

output "marsh_runner_image" {
  value       = "ghcr.io/${var.github_org_lowercase}/marsh-runner:${var.marsh_runner_image_tag}"
  description = "Full image reference; snapshots are pulled from this registry image."
}

output "snapshots" {
  value = {
    default = {
      name   = "marsh-runner-default"
      cpu    = 2
      memory = 4
      disk   = 10
    }
    large = {
      name   = "marsh-runner-large"
      cpu    = 4
      memory = 8
      disk   = 10
    }
  }
  description = "Registered runner snapshots (2 sizes from 1 image)."
}
