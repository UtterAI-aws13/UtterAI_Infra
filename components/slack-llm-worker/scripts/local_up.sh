#!/usr/bin/env bash
# Bring up the LocalStack environment and run the end-to-end scenarios.
# Needs: Docker, lstk (logged in), Terraform, Ollama serving the model below. No AWS account.
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${MODEL:-llama3.2:1b}"

curl -sf localhost:11434/api/version >/dev/null || { echo "Ollama is not running (ollama serve)"; exit 1; }
ollama list | grep -q "^${MODEL}" || ollama pull "$MODEL"

curl -sf localhost:4566/_localstack/health >/dev/null || lstk start --type aws

[ -x .venv/bin/python ] || { uv venv -q .venv && uv pip install -q -p .venv/bin/python -r requirements-test.lock; }
.venv/bin/python tools/package_lambda.py --output dist/lambda.zip

terraform -chdir=infra/terraform/envs/local init -input=false >/dev/null
terraform -chdir=infra/terraform/envs/local apply -auto-approve -input=false

.venv/bin/python e2e/run_localstack.py --model "$MODEL" "$@"
