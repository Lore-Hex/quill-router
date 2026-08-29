# This root intentionally has no input variables. These are single-account
# production constants, not deployment knobs; making them configurable would
# imply portability that this narrowly scoped state does not have.
locals {
  aws_account_id = "330422590279"
  aws_region     = "eu-west-3"

  gcp_project_id     = "quill-cloud-proxy"
  gcp_project_number = "44325983244"

  github_owner    = "Lore-Hex"
  github_owner_id = "279148885"

  quill_router_repository_id      = "1227431177"
  quill_cloud_proxy_repository_id = "1223401116"
}
