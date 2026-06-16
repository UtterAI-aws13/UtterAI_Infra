locals {
  alertmanager_receiver_name = var.alertmanager_slack_enabled ? "slack" : "null"

  alertmanager_config = {
    global = {
      resolve_timeout = "5m"
    }
    route = {
      receiver        = local.alertmanager_receiver_name
      group_by        = ["namespace", "alertname"]
      group_wait      = "30s"
      group_interval  = "5m"
      repeat_interval = "4h"
    }
    receivers = concat(
      [
        merge(
          {
            name = local.alertmanager_receiver_name
          },
          var.alertmanager_slack_enabled ? {
            slack_configs = [
              {
                api_url_file  = "/etc/alertmanager/secrets/${var.alertmanager_slack_webhook_secret_name}/${var.alertmanager_slack_webhook_secret_key}"
                channel       = var.alertmanager_slack_channel
                send_resolved = true
                title         = "[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}"
                text          = "{{ range .Alerts }}*Severity:* {{ .Labels.severity }}\n*Namespace:* {{ .Labels.namespace }}\n*Summary:* {{ .Annotations.summary }}\n*Description:* {{ .Annotations.description }}\n{{ end }}"
              }
            ]
          } : {}
        )
      ],
      var.alertmanager_slack_enabled ? [{ name = "null" }] : []
    )
  }

  alertmanager_spec = merge(
    {
      replicas = 1
      resources = {
        requests = {
          cpu    = "50m"
          memory = "128Mi"
        }
        limits = {
          cpu    = "200m"
          memory = "256Mi"
        }
      }
    },
    var.alertmanager_slack_enabled ? {
      secrets = [var.alertmanager_slack_webhook_secret_name]
    } : {}
  )
}

# ── Alertmanager Slack Secret Sync ───────────────────────────────────────────

resource "aws_secretsmanager_secret" "alertmanager_slack_webhook" {
  name                    = var.alertmanager_slack_secret_manager_name
  description             = "Slack incoming webhook URL for Alertmanager notifications."
  recovery_window_in_days = 0
}

# ── AWS Load Balancer Controller ──────────────────────────────────────────────

resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.8.1"
  namespace  = "ingress-system"

  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  set {
    name  = "clusterName"
    value = var.cluster_name
  }

  set {
    name  = "serviceAccount.create"
    value = "true"
  }

  set {
    name  = "serviceAccount.name"
    value = "aws-load-balancer-controller"
  }

  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = var.lbc_irsa_role_arn
  }

  set {
    name  = "region"
    value = var.aws_region
  }

  set {
    name  = "vpcId"
    value = var.vpc_id
  }
}

# ── EKS Observability ────────────────────────────────────────────────────────

resource "helm_release" "kube_prometheus_stack" {
  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = "66.2.1"
  namespace        = "monitoring"
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  values = [
    yamlencode({
      fullnameOverride = "utterai-monitoring"

      # EKS manages these control plane components outside worker nodes, so
      # Prometheus cannot reliably scrape their endpoints from the cluster.
      defaultRules = {
        rules = {
          etcd                   = false
          kubeControllerManager  = false
          kubeSchedulerAlerting  = false
          kubeSchedulerRecording = false
        }
      }

      prometheus = {
        prometheusSpec = {
          retention      = "7d"
          scrapeInterval = "60s"

          serviceMonitorSelectorNilUsesHelmValues = false
          podMonitorSelectorNilUsesHelmValues     = false
          ruleSelectorNilUsesHelmValues           = false
          serviceMonitorNamespaceSelector         = {}
          podMonitorNamespaceSelector             = {}
          ruleNamespaceSelector                   = {}

          resources = {
            requests = {
              cpu    = "100m"
              memory = "512Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "1Gi"
            }
          }
        }
      }

      grafana = {
        enabled = true
        service = {
          type = "ClusterIP"
        }
        defaultDashboardsTimezone = "Asia/Seoul"
        additionalDataSources = [
          {
            name      = "Loki"
            type      = "loki"
            access    = "proxy"
            url       = "http://loki-gateway.monitoring.svc.cluster.local"
            isDefault = false
          }
        ]
      }

      kubeStateMetrics = {
        enabled = true
      }

      "prometheus-node-exporter" = {
        enabled = true
      }

      kubelet = {
        enabled = true
        serviceMonitor = {
          cAdvisor = true
        }
      }

      kubeControllerManager = {
        enabled = false
      }

      kubeEtcd = {
        enabled = false
      }

      kubeScheduler = {
        enabled = false
      }

      alertmanager = {
        enabled          = true
        config           = local.alertmanager_config
        alertmanagerSpec = local.alertmanager_spec
      }
    })
  ]
}

# ── Grafana Loki / Promtail ──────────────────────────────────────────────────

resource "helm_release" "loki" {
  name             = "loki"
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "loki"
  version          = "7.0.0"
  namespace        = "monitoring"
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  depends_on = [helm_release.kube_prometheus_stack]

  values = [
    yamlencode({
      deploymentMode = "SingleBinary"

      loki = {
        auth_enabled = false
        commonConfig = {
          path_prefix        = "/tmp/loki"
          replication_factor = 1
        }
        storage = {
          type = "filesystem"
          filesystem = {
            chunks_directory = "/tmp/loki/chunks"
            rules_directory  = "/tmp/loki/rules"
          }
        }
        storage_config = {
          bloom_shipper = {
            working_directory = "/tmp/loki/data/bloomshipper"
          }
        }
        rulerConfig = {
          wal = {
            dir = "/tmp/loki/ruler-wal"
          }
        }
        useTestSchema = true
      }

      singleBinary = {
        replicas = 1
        resources = {
          requests = {
            cpu    = "100m"
            memory = "256Mi"
          }
          limits = {
            cpu    = "500m"
            memory = "1Gi"
          }
        }
        persistence = {
          enabled = false
        }
      }

      read = {
        replicas = 0
      }
      write = {
        replicas = 0
      }
      backend = {
        replicas = 0
      }

      chunksCache = {
        enabled = false
      }
      resultsCache = {
        enabled = false
      }
      lokiCanary = {
        enabled = false
      }
      test = {
        enabled = false
      }
    })
  ]
}

resource "helm_release" "promtail" {
  name             = "promtail"
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "promtail"
  version          = "6.17.1"
  namespace        = "monitoring"
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true

  depends_on = [helm_release.loki]

  values = [
    yamlencode({
      config = {
        clients = [
          {
            url = "http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/push"
          }
        ]
      }
      resources = {
        requests = {
          cpu    = "50m"
          memory = "64Mi"
        }
        limits = {
          cpu    = "200m"
          memory = "256Mi"
        }
      }
      tolerations = [
        {
          key      = "node-role.kubernetes.io/master"
          operator = "Exists"
          effect   = "NoSchedule"
        },
        {
          key      = "node-role.kubernetes.io/control-plane"
          operator = "Exists"
          effect   = "NoSchedule"
        },
        {
          key      = "dedicated"
          operator = "Equal"
          value    = "ai-gpu"
          effect   = "NoSchedule"
        }
      ]
    })
  ]
}

# ── Cluster Autoscaler ────────────────────────────────────────────────────────

resource "helm_release" "cluster_autoscaler" {
  name            = "cluster-autoscaler"
  repository      = "https://kubernetes.github.io/autoscaler"
  chart           = "cluster-autoscaler"
  version         = "9.37.0"
  namespace       = "kube-system"
  cleanup_on_fail = true

  depends_on = [
    helm_release.aws_load_balancer_controller,
    helm_release.kube_prometheus_stack,
  ]

  set {
    name  = "autoDiscovery.clusterName"
    value = var.cluster_name
  }

  set {
    name  = "awsRegion"
    value = var.aws_region
  }

  set {
    name  = "rbac.serviceAccount.name"
    value = "cluster-autoscaler"
  }

  set {
    name  = "rbac.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = var.cluster_autoscaler_irsa_role_arn
  }

  set {
    name  = "extraArgs.balance-similar-node-groups"
    value = "true"
  }

  set {
    name  = "extraArgs.skip-nodes-with-system-pods"
    value = "false"
  }

  set {
    name  = "resources.requests.cpu"
    value = "100m"
  }

  set {
    name  = "resources.requests.memory"
    value = "256Mi"
  }

  set {
    name  = "serviceMonitor.enabled"
    value = "true"
  }

  set {
    name  = "serviceMonitor.interval"
    value = "60s"
  }

  set {
    name  = "tolerations[0].key"
    value = "CriticalAddonsOnly"
  }

  set {
    name  = "tolerations[0].operator"
    value = "Exists"
  }

  set {
    name  = "tolerations[0].effect"
    value = "NoSchedule"
  }
}

# ── Metrics Server ────────────────────────────────────────────────────────────

resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  version    = "3.12.1"
  namespace  = "kube-system"

  depends_on = [helm_release.aws_load_balancer_controller]

  set {
    name  = "tolerations[0].key"
    value = "CriticalAddonsOnly"
  }

  set {
    name  = "tolerations[0].operator"
    value = "Exists"
  }

  set {
    name  = "tolerations[0].effect"
    value = "NoSchedule"
  }
}

# ── External Secrets Operator ────────────────────────────────────────────────

resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  version    = "0.10.4"
  namespace  = "external-secrets"

  create_namespace = true

  depends_on = [helm_release.aws_load_balancer_controller]

  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = var.eso_irsa_role_arn
  }
}

resource "kubernetes_manifest" "alertmanager_slack_webhook" {
  manifest = {
    apiVersion = "external-secrets.io/v1beta1"
    kind       = "ExternalSecret"
    metadata = {
      name      = "alertmanager-slack-webhook"
      namespace = "monitoring"
    }
    spec = {
      refreshInterval = "1h"
      secretStoreRef = {
        name = var.external_secrets_cluster_store_name
        kind = "ClusterSecretStore"
      }
      target = {
        name           = var.alertmanager_slack_webhook_secret_name
        creationPolicy = "Owner"
      }
      data = [
        {
          secretKey = var.alertmanager_slack_webhook_secret_key
          remoteRef = {
            key = aws_secretsmanager_secret.alertmanager_slack_webhook.name
          }
        }
      ]
    }
  }

  depends_on = [
    helm_release.kube_prometheus_stack,
    helm_release.external_secrets,
  ]
}

# ── NVIDIA Device Plugin ──────────────────────────────────────────────────────

resource "helm_release" "nvidia_device_plugin" {
  name       = "nvidia-device-plugin"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = "0.16.2"
  namespace  = "kube-system"

  depends_on = [helm_release.aws_load_balancer_controller]

  values = [
    yamlencode({
      affinity = {
        nodeAffinity = {
          requiredDuringSchedulingIgnoredDuringExecution = {
            nodeSelectorTerms = [{
              matchExpressions = [{
                key      = "workload"
                operator = "In"
                values   = ["ai-gpu"]
              }]
            }]
          }
        }
      }
      tolerations = [{
        key      = "dedicated"
        operator = "Equal"
        value    = "ai-gpu"
        effect   = "NoSchedule"
      }]
    })
  ]
}

# ── ArgoCD ──────────────────────────────────────────────────────

resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = "9.5.20"
  namespace  = "argocd"

  create_namespace = true

  depends_on = [helm_release.aws_load_balancer_controller]

  values = [
    yamlencode({
      server = {
        service = {
          type = "ClusterIP"
        }
      }
    })
  ]
}
