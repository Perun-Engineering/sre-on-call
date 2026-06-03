# Remote backend for team collaboration and state locking. Uncomment and fill
# in, then `terraform init -migrate-state`. Left commented so the example runs
# with local state out of the box.
#
# terraform {
#   backend "s3" {
#     bucket       = "your-terraform-state-bucket"
#     key          = "sre-on-call/terraform.tfstate"
#     region       = "us-east-1"
#     encrypt      = true
#     use_lockfile = true
#   }
# }
