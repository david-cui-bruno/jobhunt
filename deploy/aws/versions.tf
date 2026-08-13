terraform {
  required_version = ">= 1.7.0"

  # The bucket is created by the first local-state apply. Immediately afterward,
  # migrate state with the backend settings documented in README.md.
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}
