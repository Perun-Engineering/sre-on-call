###############################################################################
# sre-on-call module — Provider requirements
#
# A reusable module: it declares only provider *requirements*. The calling
# root module is responsible for the `provider "aws"` configuration (region,
# credentials, default_tags) and the backend. See examples/complete.
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.21"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}
