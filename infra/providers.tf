terraform {
  required_providers {
    # CI's terraform validate confirms compatibility within this major line.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }

    # CI's terraform validate confirms compatibility within this major line.
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "aws" {
  region = local.aws_region
}

provider "google" {
  project = local.gcp_project_id
}
