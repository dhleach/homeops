terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment and configure after first apply to store state in S3:
  # backend "s3" {
  #   bucket = "homeops-terraform-state"
  #   key    = "homeops/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

# Primary region — all resources except ACM cert
provider "aws" {
  region = var.aws_region
}

# ACM cert must be in us-east-1 for CloudFront
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}
