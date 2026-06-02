locals {
  prefix = "${var.project_name}-${var.environment}"

  oidc_sub = "${var.oidc_provider_url}:sub"
  oidc_aud = "${var.oidc_provider_url}:aud"
}

# ── AWS Load Balancer Controller IRSA ────────────────────────────────────────

resource "aws_iam_role" "lbc" {
  name = "${local.prefix}-lbc-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:ingress-system:aws-load-balancer-controller"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lbc" {
  role       = aws_iam_role.lbc.name
  policy_arn = aws_iam_policy.lbc.arn
}

resource "aws_iam_policy" "lbc" {
  name   = "${local.prefix}-lbc-policy"
  policy = file("${path.module}/policies/lbc-policy.json")
}

# ── Karpenter Node Role (EC2 인스턴스용) ──────────────────────────────────────

resource "aws_iam_role" "karpenter_node" {
  name = "${local.prefix}-karpenter-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "karpenter_node_worker" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_cni" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_ecr" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_ssm" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "karpenter_node" {
  name = "${local.prefix}-karpenter-node-profile"
  role = aws_iam_role.karpenter_node.name
}

# ── Karpenter Controller IRSA ─────────────────────────────────────────────────

resource "aws_sqs_queue" "karpenter_interruption" {
  name                      = "${local.prefix}-karpenter-interruption"
  message_retention_seconds = 300
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_policy" "karpenter_interruption" {
  queue_url = aws_sqs_queue.karpenter_interruption.url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = ["events.amazonaws.com", "sqs.amazonaws.com"] }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.karpenter_interruption.arn
      }
    ]
  })
}

resource "aws_iam_role" "karpenter_controller" {
  name = "${local.prefix}-karpenter-controller-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:karpenter:karpenter"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "karpenter_controller" {
  name = "${local.prefix}-karpenter-controller-policy"
  role = aws_iam_role.karpenter_controller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowScopedEC2InstanceActions"
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:CreateFleet",
          "ec2:CreateLaunchTemplate",
        ]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "aws:RequestedRegion"                    = var.aws_region
            "ec2:ResourceTag/karpenter.sh/discovery" = var.cluster_name
          }
        }
      },
      {
        Sid    = "AllowScopedEC2InstanceTermination"
        Effect = "Allow"
        Action = [
          "ec2:TerminateInstances",
          "ec2:DeleteLaunchTemplate",
        ]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "aws:RequestedRegion"                     = var.aws_region
            "ec2:ResourceTag/karpenter.sh/managed-by" = var.cluster_name
          }
        }
      },
      {
        Sid    = "AllowEC2ReadActions"
        Effect = "Allow"
        Action = [
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplates",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSpotPriceHistory",
          "ec2:DescribeSubnets",
        ]
        Resource = ["*"]
      },
      {
        Sid      = "AllowSSMReadActions"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = ["arn:aws:ssm:*:*:parameter/aws/service/*"]
      },
      {
        Sid      = "AllowPassNodeIAMRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.karpenter_node.arn]
      },
      {
        Sid    = "AllowInterruptionQueueActions"
        Effect = "Allow"
        Action = [
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ReceiveMessage",
        ]
        Resource = [aws_sqs_queue.karpenter_interruption.arn]
      },
      {
        Sid    = "AllowEKSReadActions"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:DescribeNodegroup",
        ]
        Resource = ["*"]
      },
    ]
  })
}

# ── KEDA IRSA ─────────────────────────────────────────────────────────────────

resource "aws_iam_role" "keda" {
  name = "${local.prefix}-keda-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:keda:keda-operator"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "keda" {
  name = "${local.prefix}-keda-policy"
  role = aws_iam_role.keda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ListQueues",
      ]
      Resource = [
        var.analysis_queue_arn,
        var.dlq_arn,
      ]
    }]
  })
}

# ── API IRSA ──────────────────────────────────────────────────────────────────

resource "aws_iam_role" "api" {
  name = "${local.prefix}-api-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-api:utterai-api-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "api" {
  name = "${local.prefix}-api-policy"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:HeadObject", "s3:DeleteObject"]
        Resource = [
          "${var.raw_audio_bucket_arn}/*",
          "${var.reports_bucket_arn}/*",
          "${var.artifacts_bucket_arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [var.analysis_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${local.prefix}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
      },
    ]
  })
}

# ── AI CPU Worker IRSA ────────────────────────────────────────────────────────

resource "aws_iam_role" "ai_cpu" {
  name = "${local.prefix}-ai-cpu-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-ai-cpu:utterai-cpu-worker-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ai_cpu" {
  name = "${local.prefix}-ai-cpu-policy"
  role = aws_iam_role.ai_cpu.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = [var.analysis_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${var.raw_audio_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${var.processed_audio_bucket_arn}/*"]
      },
    ]
  })
}

# ── AI GPU Worker IRSA ────────────────────────────────────────────────────────

resource "aws_iam_role" "ai_gpu" {
  name = "${local.prefix}-ai-gpu-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-ai-gpu:utterai-gpu-worker-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ai_gpu" {
  name = "${local.prefix}-ai-gpu-policy"
  role = aws_iam_role.ai_gpu.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = [var.analysis_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${var.raw_audio_bucket_arn}/*", "${var.processed_audio_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${var.processed_audio_bucket_arn}/*", "${var.artifacts_bucket_arn}/*"]
      },
    ]
  })
}

# ── Batch Worker IRSA ─────────────────────────────────────────────────────────

resource "aws_iam_role" "batch" {
  name = "${local.prefix}-batch-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-batch:utterai-batch-worker-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "batch" {
  name = "${local.prefix}-batch-policy"
  role = aws_iam_role.batch.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = [var.analysis_queue_arn, var.dlq_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${var.reports_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${local.prefix}/db-password*"]
      },
    ]
  })
}
