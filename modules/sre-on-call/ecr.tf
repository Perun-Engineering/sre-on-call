###############################################################################
# sre-on-call — ECR Repositories
###############################################################################

locals {
  agent_image_names = [
    "${var.project_name}-master",
    "${var.project_name}-slack-scanner",
    "${var.project_name}-discord-scanner",
    "${var.project_name}-cloudwatch-logs",
    "${var.project_name}-eks",
    "${var.project_name}-incident-history",
  ]
}

resource "aws_ecr_repository" "agents" {
  for_each = toset(local.agent_image_names)

  name                 = each.key
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Agent = trimprefix(each.key, "${var.project_name}-")
  }
}

resource "aws_ecr_lifecycle_policy" "agents" {
  for_each = aws_ecr_repository.agents

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
    ]
  })
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "ecr_repository_uris" {
  description = "Map of agent name -> ECR repository URI"
  value       = { for k, v in aws_ecr_repository.agents : k => v.repository_url }
}

output "ecr_registry_url" {
  description = "ECR registry URL (account.dkr.ecr.region.amazonaws.com) for agent_container_registry tfvar"
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}
