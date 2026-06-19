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

# ── Cluster Autoscaler IRSA ───────────────────────────────────────────────────

resource "aws_iam_role" "cluster_autoscaler" {
  name = "${local.prefix}-cluster-autoscaler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:kube-system:cluster-autoscaler"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  name = "${local.prefix}-cluster-autoscaler-policy"
  role = aws_iam_role.cluster_autoscaler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeAutoScalingInstances",
        "autoscaling:DescribeLaunchConfigurations",
        "autoscaling:DescribeScalingActivities",
        "autoscaling:SetDesiredCapacity",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        "ec2:DescribeImages",
        "ec2:DescribeLaunchTemplateVersions",
        "ec2:GetInstanceTypesFromInstanceRequirements",
        "eks:DescribeNodegroup",
      ]
      Resource = ["*"]
    }]
  })
}

# ── AI API IRSA ───────────────────────────────────────────────────────────────

resource "aws_iam_role" "ai_api" {
  name = "${local.prefix}-ai-api-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-ai-api:utterai-ai-api-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ai_api" {
  name = "${local.prefix}-ai-api-policy"
  role = aws_iam_role.ai_api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = [var.audio_preprocess_queue_arn]
      },
    ]
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
          "${var.template_bucket_arn}/*",
          "${var.rag_ingest_bucket_arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [var.audio_preprocess_queue_arn, var.report_analysis_queue_arn]
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
        Resource = [var.audio_preprocess_queue_arn, var.audio_preprocess_dlq_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [var.gpu_inference_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = [var.report_analysis_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${var.raw_audio_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${var.reports_bucket_arn}/*"]
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:inference-profile/*",
        ]
      },
    ]
  })
}

# ── AI ML GPU Worker IRSA ─────────────────────────────────────────────────────

resource "aws_iam_role" "ai_ml_gpu" {
  name = "${local.prefix}-ai-ml-gpu-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-ai-gpu:utterai-ml-gpu-worker-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ai_ml_gpu" {
  name = "${local.prefix}-ai-ml-gpu-policy"
  role = aws_iam_role.ai_ml_gpu.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = [var.gpu_inference_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [var.report_analysis_queue_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${var.raw_audio_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${var.reports_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${local.prefix}/*"]
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
        Resource = [var.rag_ingest_queue_arn, var.rag_ingest_dlq_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${var.rag_ingest_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${var.reports_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${local.prefix}/*"]
      },
    ]
  })
}

# ── External Secrets Operator IRSA ───────────────────────────────────────────

resource "aws_iam_role" "eso" {
  name = "${local.prefix}-eso-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:external-secrets:external-secrets"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "eso" {
  name = "${local.prefix}-eso-policy"
  role = aws_iam_role.eso.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${local.prefix}/*",
          "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:rds!db-*",
        ]
      },
    ]
  })
}

# ── Kubecost IRSA ────────────────────────────────────────────────────────────

resource "aws_iam_role" "kubecost" {
  name = "${local.prefix}-kubecost-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:kubecost:kubecost"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "kubecost" {
  name = "${local.prefix}-kubecost-policy"
  role = aws_iam_role.kubecost.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = [
          var.kubecost_bucket_arn,
          "${var.kubecost_bucket_arn}/*"
        ]
      }
    ]
  })
}

# ── Karpenter IRSA ───────────────────────────────────────────────────────────

resource "aws_iam_role" "karpenter" {
  name = "${local.prefix}-karpenter-irsa-role"

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

resource "aws_iam_role_policy" "karpenter" {
  name = "${local.prefix}-karpenter-policy"
  role = aws_iam_role.karpenter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateFleet",
          "ec2:CreateLaunchTemplate",
          "ec2:CreateTags",
          "ec2:DeleteLaunchTemplate",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplates",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSpotPriceHistory",
          "ec2:DescribeSubnets",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
        ]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = ["arn:aws:iam::${var.aws_account_id}:role/${local.prefix}-eks-node-role"]
      },
      {
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster"]
        Resource = ["arn:aws:eks:${var.aws_region}:${var.aws_account_id}:cluster/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = ["arn:aws:ssm:*:*:parameter/aws/service/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["pricing:GetProducts"]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ReceiveMessage",
        ]
        Resource = ["arn:aws:sqs:${var.aws_region}:${var.aws_account_id}:${var.cluster_name}"]
      },
      {
        Effect   = "Allow"
        Action   = ["iam:CreateInstanceProfile", "iam:TagInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile"]
        Resource = ["*"]
      },
    ]
  })
}

# ── KEDA IRSA ────────────────────────────────────────────────────────────────

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
    Statement = [
      {
        Effect = "Allow"
        Action = ["sqs:GetQueueAttributes"]
        Resource = [
          var.audio_preprocess_queue_arn,
          var.gpu_inference_queue_arn,
          var.report_analysis_queue_arn,
          var.rag_ingest_queue_arn,
        ]
      },
    ]
  })
}

# ── EFS CSI Driver IRSA ──────────────────────────────────────────────────────

resource "aws_iam_role" "efs_csi_driver" {
  name = "${local.prefix}-efs-csi-driver-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:kube-system:efs-csi-controller-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "efs_csi_driver" {
  name = "${local.prefix}-efs-csi-driver-policy"
  role = aws_iam_role.efs_csi_driver.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "elasticfilesystem:DescribeAccessPoints",
          "elasticfilesystem:DescribeFileSystems",
          "elasticfilesystem:DescribeMountTargets",
          "ec2:DescribeAvailabilityZones",
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "elasticfilesystem:CreateAccessPoint",
          "elasticfilesystem:TagResource",
        ]
        Resource = ["*"]
        Condition = {
          StringLike = {
            "aws:RequestTag/efs.csi.aws.com/cluster" = "true"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticfilesystem:DeleteAccessPoint"]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "aws:ResourceTag/efs.csi.aws.com/cluster" = "true"
          }
        }
      },
    ]
  })
}

# ── Loki IRSA ─────────────────────────────────────────────────────────────────

resource "aws_iam_role" "loki" {
  name = "${local.prefix}-loki-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:monitoring:loki"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "loki" {
  name = "${local.prefix}-loki-policy"
  role = aws_iam_role.loki.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:GetObjectVersion"
        ]
        Resource = [
          var.loki_bucket_arn,
          "${var.loki_bucket_arn}/*"
        ]
      }
    ]
  })
}

# ── Tempo IRSA ────────────────────────────────────────────────────────────────

resource "aws_iam_role" "tempo" {
  count = var.tempo_bucket_arn != "" ? 1 : 0

  name = "${local.prefix}-tempo-irsa-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:monitoring:tempo"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "tempo" {
  count = var.tempo_bucket_arn != "" ? 1 : 0

  name = "${local.prefix}-tempo-policy"
  role = aws_iam_role.tempo[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:GetObjectVersion"
        ]
        Resource = [
          var.tempo_bucket_arn,
          "${var.tempo_bucket_arn}/*"
        ]
      }
    ]
  })
}
