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
      routes = [
        {
          receiver = "null"
          matchers = [
            "alertname = Watchdog"
          ]
        }
      ]
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

resource "aws_secretsmanager_secret" "grafana_admin_credentials" {
  name                    = var.grafana_admin_secret_manager_name
  description             = "Grafana admin credentials for the kube-prometheus-stack Grafana instance."
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

  depends_on = [kubernetes_manifest.grafana_admin_credentials]

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
          retention      = "3d"
          scrapeInterval = "60s"

          serviceMonitorSelectorNilUsesHelmValues = false
          podMonitorSelectorNilUsesHelmValues     = false
          ruleSelectorNilUsesHelmValues           = false
          serviceMonitorNamespaceSelector         = {}
          podMonitorNamespaceSelector             = {}
          ruleNamespaceSelector                   = {}

          resources = {
            requests = {
              cpu    = "200m"
              memory = "1Gi"
            }
            limits = {
              cpu    = "1"
              memory = "3Gi"
            }
          }
        }
      }

      grafana = merge(
        {
          enabled = true
          service = {
            type = "ClusterIP"
          }
          defaultDashboardsTimezone = "Asia/Seoul"
          additionalDataSources = [
            {
              uid       = "loki"
              name      = "Loki"
              type      = "loki"
              access    = "proxy"
              url       = "http://loki-gateway.monitoring.svc.cluster.local"
              isDefault = false
            },
            {
              uid       = "tempo"
              name      = "Tempo"
              type      = "tempo"
              access    = "proxy"
              url       = "http://tempo.monitoring.svc.cluster.local:3100"
              isDefault = false
            }
          ]
        },
        var.grafana_admin_credentials_enabled ? {
          admin = {
            existingSecret = var.grafana_admin_kubernetes_secret_name
            userKey        = var.grafana_admin_user_key
            passwordKey    = var.grafana_admin_password_key
          }
        } : {}
      )

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

# ── Kubecost ─────────────────────────────────────────────────────────────────

resource "helm_release" "kubecost" {
  count = var.kubecost_enabled ? 1 : 0

  name             = "kubecost"
  repository       = "https://kubecost.github.io/cost-analyzer/"
  chart            = "cost-analyzer"
  version          = var.kubecost_chart_version
  namespace        = var.kubecost_namespace
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true
  timeout          = 600

  depends_on = [helm_release.kube_prometheus_stack]

  values = [
    yamlencode({
      fullnameOverride = "kubecost"

      global = {
        clusterId = var.cluster_name
        prometheus = {
          enabled = false
          fqdn    = "http://utterai-monitoring-prometheus.monitoring.svc.cluster.local:9090"
        }
        grafana = {
          enabled    = false
          domainName = "kube-prometheus-stack-grafana.monitoring.svc.cluster.local"
          proxy      = false
        }
      }

      service = {
        type = "ClusterIP"
      }

      persistentVolume = {
        enabled = var.kubecost_persistent_volume_enabled
        size    = var.kubecost_persistent_volume_size
      }

      kubecostFrontend = {
        resources = {
          requests = {
            cpu    = "10m"
            memory = "64Mi"
          }
          limits = {
            cpu    = "200m"
            memory = "256Mi"
          }
        }
      }

      serviceAccount = {
        create = true
        name   = "kubecost"
        annotations = {
          "eks.amazonaws.com/role-arn" = var.kubecost_irsa_role_arn
        }
      }

      kubecostModel = {
        etlDailyStoreDurationDays   = var.kubecost_etl_daily_store_duration_days
        etlHourlyStoreDurationHours = var.kubecost_etl_hourly_store_duration_hours
        maxQueryConcurrency         = 3
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
        thanos = {
          objectStore = {
            enabled = var.kubecost_s3_bucket_name != "" ? true : false
            config = var.kubecost_s3_bucket_name != "" ? yamlencode({
              type = "S3"
              config = {
                bucket   = var.kubecost_s3_bucket_name
                endpoint = "s3.${var.aws_region}.amazonaws.com"
              }
            }) : ""
          }
        }
      }

      kubecostAggregator = {
        deployMethod = "singlepod"
      }

      prometheus = {
        server = {
          enabled = false
        }
      }

      grafana = {
        enabled = false
      }

      serviceMonitor = {
        enabled       = true
        interval      = "60s"
        scrapeTimeout = "30s"
        aggregatorMetrics = {
          enabled       = true
          interval      = "60s"
          scrapeTimeout = "30s"
        }
      }

      prometheusRule = {
        enabled = false
      }

      networkCosts = {
        enabled = false
      }

      forecasting = {
        enabled = false
      }

      diagnostics = {
        enabled = false
      }
    })
  ]
}

# ── Grafana Tempo ────────────────────────────────────────────────────────────

resource "helm_release" "tempo" {
  name             = "tempo"
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "tempo"
  version          = var.tempo_chart_version
  namespace        = "monitoring"
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  depends_on = [helm_release.kube_prometheus_stack]

  values = [
    yamlencode({
      fullnameOverride = "tempo"

      serviceAccount = {
        create = true
        name   = "tempo"
        annotations = {
          "eks.amazonaws.com/role-arn" = var.tempo_irsa_role_arn
        }
      }

      tempo = {
        reportingEnabled = false
        retention        = var.tempo_retention_period
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
        storage = {
          trace = {
            backend = "s3"
            s3 = {
              bucket   = var.tempo_s3_bucket_name
              endpoint = "s3.${var.aws_region}.amazonaws.com"
            }
            wal = {
              path = "/var/tempo/wal"
            }
          }
        }
        receivers = {
          otlp = {
            protocols = {
              grpc = {
                endpoint = "0.0.0.0:4317"
              }
              http = {
                endpoint = "0.0.0.0:4318"
              }
            }
          }
        }
      }

      persistence = {
        enabled = false
      }

      tempoQuery = {
        enabled = false
      }

      service = {
        type = "ClusterIP"
      }

      serviceMonitor = {
        enabled  = true
        interval = "60s"
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

      serviceAccount = {
        create = true
        name   = "loki"
        annotations = {
          "eks.amazonaws.com/role-arn" = var.loki_irsa_role_arn
        }
      }

      loki = {
        auth_enabled = false
        commonConfig = {
          path_prefix        = "/tmp/loki"
          replication_factor = 1
        }
        storage = {
          type = "s3"
          bucketNames = {
            chunks = var.loki_s3_bucket_name
            ruler  = var.loki_s3_bucket_name
            admin  = var.loki_s3_bucket_name
          }
          s3 = {
            region = var.aws_region
          }
        }
        limits_config = {
          retention_period = var.loki_retention_period
        }
        compactor = {
          retention_enabled    = true
          delete_request_store = "s3"
          working_directory    = "/tmp/loki/compactor"
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
          value    = "api"
          effect   = "NoSchedule"
        },
        {
          key      = "dedicated"
          operator = "Equal"
          value    = "worker"
          effect   = "NoSchedule"
        },
        {
          key      = "dedicated"
          operator = "Equal"
          value    = "ai-gpu"
          effect   = "NoSchedule"
        },
        {
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }
      ]
    })
  ]
}

# ── Cluster Autoscaler ────────────────────────────────────────────────────────

resource "helm_release" "cluster_autoscaler" {
  count           = var.cluster_autoscaler_enabled ? 1 : 0
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
    helm_release.external_secrets,
  ]
}

resource "kubernetes_manifest" "grafana_admin_credentials" {
  count = var.grafana_admin_credentials_enabled ? 1 : 0

  manifest = {
    apiVersion = "external-secrets.io/v1beta1"
    kind       = "ExternalSecret"
    metadata = {
      name      = var.grafana_admin_kubernetes_secret_name
      namespace = "monitoring"
    }
    spec = {
      refreshInterval = "1h"
      secretStoreRef = {
        name = var.external_secrets_cluster_store_name
        kind = "ClusterSecretStore"
      }
      target = {
        name           = var.grafana_admin_kubernetes_secret_name
        creationPolicy = "Owner"
      }
      data = [
        {
          secretKey = var.grafana_admin_user_key
          remoteRef = {
            key      = aws_secretsmanager_secret.grafana_admin_credentials.name
            property = replace(var.grafana_admin_user_key, "-", "_")
          }
        },
        {
          secretKey = var.grafana_admin_password_key
          remoteRef = {
            key      = aws_secretsmanager_secret.grafana_admin_credentials.name
            property = replace(var.grafana_admin_password_key, "-", "_")
          }
        }
      ]
    }
  }

  depends_on = [
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
      tolerations = [
        {
          key      = "dedicated"
          operator = "Equal"
          value    = "ai-gpu"
          effect   = "NoSchedule"
        },
        {
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }
      ]
    })
  ]
}

# ── KEDA ─────────────────────────────────────────────────────────────────────

resource "helm_release" "keda" {
  count = var.keda_enabled ? 1 : 0

  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = "2.16.1"
  namespace        = "keda"
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  depends_on = [helm_release.aws_load_balancer_controller]

  values = [
    yamlencode({
      serviceAccount = {
        operator = {
          annotations = {
            "eks.amazonaws.com/role-arn" = var.keda_irsa_role_arn
          }
        }
      }
      resources = {
        operator = {
          requests = { cpu = "50m", memory = "128Mi" }
          limits   = { cpu = "500m", memory = "512Mi" }
        }
        metricServer = {
          requests = { cpu = "50m", memory = "64Mi" }
          limits   = { cpu = "200m", memory = "256Mi" }
        }
      }
      tolerations = [
        {
          key      = "CriticalAddonsOnly"
          operator = "Exists"
          effect   = "NoSchedule"
        }
      ]
    })
  ]
}

# ── Karpenter ─────────────────────────────────────────────────────────────────

resource "helm_release" "karpenter" {
  count = var.karpenter_enabled && var.karpenter_irsa_role_arn != "" ? 1 : 0

  name             = "karpenter"
  repository       = "oci://public.ecr.aws/karpenter"
  chart            = "karpenter"
  version          = "1.3.3"
  namespace        = "karpenter"
  create_namespace = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  depends_on = [helm_release.aws_load_balancer_controller]

  values = [
    yamlencode({
      serviceAccount = {
        annotations = {
          "eks.amazonaws.com/role-arn" = var.karpenter_irsa_role_arn
        }
      }
      settings = {
        clusterName       = var.cluster_name
        clusterEndpoint   = var.cluster_endpoint
        interruptionQueue = var.cluster_name
      }
      resources = {
        requests = { cpu = "100m", memory = "256Mi" }
        limits   = { cpu = "500m", memory = "1Gi" }
      }
      tolerations = [
        {
          key      = "CriticalAddonsOnly"
          operator = "Exists"
          effect   = "NoSchedule"
        }
      ]
      controller = {
        resources = {
          requests = { cpu = "100m", memory = "256Mi" }
          limits   = { cpu = "500m", memory = "1Gi" }
        }
      }
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
