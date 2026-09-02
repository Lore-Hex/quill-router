terraform {
  backend "gcs" {
    bucket = "tr-infra-tfstate-quill-cloud-proxy"
    prefix = "infra"
  }
}
