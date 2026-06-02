# ── AWS Load Balancer Controller ──────────────────────────────────────────────

resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.8.1"
  namespace  = "ingress-system"

  create_namespace = true

  set = [
    {
      name  = "clusterName"
      value = var.cluster_name
    },
    {
      name  = "serviceAccount.create"
      value = "true"
    },
    {
      name  = "serviceAccount.name"
      value = "aws-load-balancer-controller"
    },
    {
      name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
      value = var.lbc_irsa_role_arn
    },
    {
      name  = "region"
      value = var.aws_region
    },
  ]
}

# ── Karpenter ─────────────────────────────────────────────────────────────────

resource "helm_release" "karpenter" {
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = "1.0.6"
  namespace  = "karpenter"

  create_namespace = true

  set = [
    {
      name  = "settings.clusterName"
      value = var.cluster_name
    },
    {
      name  = "settings.clusterEndpoint"
      value = var.cluster_endpoint
    },
    {
      name  = "settings.interruptionQueue"
      value = var.karpenter_sqs_queue_url
    },
    {
      name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
      value = var.karpenter_irsa_role_arn
    },
    {
      name  = "controller.resources.requests.cpu"
      value = "250m"
    },
    {
      name  = "controller.resources.requests.memory"
      value = "512Mi"
    },
    {
      name  = "controller.resources.limits.cpu"
      value = "1"
    },
    {
      name  = "controller.resources.limits.memory"
      value = "1Gi"
    },
    {
      name  = "tolerations[0].key"
      value = "CriticalAddonsOnly"
    },
    {
      name  = "tolerations[0].operator"
      value = "Exists"
    },
    {
      name  = "tolerations[0].effect"
      value = "NoSchedule"
    },
  ]
}

# ── KEDA ──────────────────────────────────────────────────────────────────────

resource "helm_release" "keda" {
  name       = "keda"
  repository = "https://kedacore.github.io/charts"
  chart      = "keda"
  version    = "2.15.1"
  namespace  = "keda"

  create_namespace = true

  set = [
    {
      name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
      value = var.keda_irsa_role_arn
    },
    {
      name  = "resources.operator.requests.cpu"
      value = "100m"
    },
    {
      name  = "resources.operator.requests.memory"
      value = "256Mi"
    },
    {
      name  = "operator.tolerations[0].key"
      value = "CriticalAddonsOnly"
    },
    {
      name  = "operator.tolerations[0].operator"
      value = "Exists"
    },
    {
      name  = "operator.tolerations[0].effect"
      value = "NoSchedule"
    },
  ]
}

# ── Metrics Server ────────────────────────────────────────────────────────────

resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  version    = "3.12.1"
  namespace  = "kube-system"

  set = [
    {
      name  = "tolerations[0].key"
      value = "CriticalAddonsOnly"
    },
    {
      name  = "tolerations[0].operator"
      value = "Exists"
    },
    {
      name  = "tolerations[0].effect"
      value = "NoSchedule"
    },
  ]
}

# ── NVIDIA Device Plugin ──────────────────────────────────────────────────────

resource "helm_release" "nvidia_device_plugin" {
  name       = "nvidia-device-plugin"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = "0.16.2"
  namespace  = "kube-system"

  set = [
    {
      name  = "tolerations[0].key"
      value = "dedicated"
    },
    {
      name  = "tolerations[0].operator"
      value = "Equal"
    },
    {
      name  = "tolerations[0].value"
      value = "ai-gpu"
    },
    {
      name  = "tolerations[0].effect"
      value = "NoSchedule"
    },
  ]
}
