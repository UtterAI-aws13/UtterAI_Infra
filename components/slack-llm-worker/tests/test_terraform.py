import re
from pathlib import Path

TF = Path(__file__).parents[1] / "infra/terraform"
MODULE = (TF / "modules/slack_worker/main.tf").read_text()
LOCAL = (TF / "envs/local/main.tf").read_text()


def test_module_is_fail_closed_and_scoped():
    assert not re.search(r'resource "aws_(ecs_service|nat_gateway|eks_cluster)"', MODULE)
    assert 'ENABLE_PAID_MODEL    = "disabled"' in MODULE
    assert MODULE.count("sqs_managed_sse_enabled   = true") + MODULE.count("sqs_managed_sse_enabled    = true") == 2
    actions = re.findall(r"actions\s*=\s*\[([^\]]*)\]", MODULE)
    assert actions and all('"*"' not in a and ':*"' not in a for a in actions)
    assert 'resources = ["*"]' not in MODULE


def test_dlq_outlives_queue_and_redrive_is_bounded():
    dlq = re.search(r'resource "aws_sqs_queue" "dlq" \{(.*?)\n\}', MODULE, re.S).group(1)
    jobs = re.search(r'resource "aws_sqs_queue" "jobs" \{(.*?)\n\}', MODULE, re.S).group(1)
    retention = lambda block: int(re.search(r"message_retention_seconds\s*=\s*(\d+)", block).group(1))
    assert retention(dlq) > retention(jobs)
    variables = (TF / "modules/slack_worker/variables.tf").read_text()
    assert "var.max_receive_count >= 3" in variables


def test_local_env_cannot_target_real_aws():
    assert 'allowed_account_ids         = ["000000000000"]' in LOCAL
    endpoints = re.search(r"endpoints \{(.*?)\}", LOCAL, re.S).group(1)
    assert all("local.endpoint" in line for line in endpoints.strip().splitlines())
    assert 'endpoint = "http://localhost:4566"' in LOCAL



def test_public_function_url_has_both_invoke_statements():
    # LocalStack accepts a URL without these; real AWS answers 403 since Oct 2025.
    assert 'function_url_auth_type = "NONE"' in MODULE
    assert "invoked_via_function_url = true" in MODULE
