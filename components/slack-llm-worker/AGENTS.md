# Slack LLM Worker — infra-lab component

- This Slack asynchronous LLM worker lives in `UtterAI_Infra/components/slack-llm-worker` on the `eunyoung/infra-lab` branch. Keep changes inside this directory unless the user asks otherwise; do not modify UtterAI application repositories.
- Git: local commits on `eunyoung/infra-lab` are allowed. Do not push, create PRs, create issues or publish without a new explicit user request.
- Local reads, code changes and offline verification are authorized. Local emulation uses LocalStack (`lstk`), Ollama and Docker Compose.
- Obtain explicit user approval before creating billable AWS resources, changing real infrastructure, calling paid models or accessing external Slack/AWS services. Never print secrets, auth tokens or response_url values.
- Model family preference for cloud: Bedrock Claude Haiku or Sonnet. Exact version, region and total budget remain undecided.
- Read README.md and docs/verification.md before describing completion or operational guarantees. Do not present local emulation results as AWS production results.
- Run tests with `.venv/bin/python -m pytest -q`. The loopback HTTP test requires permission to bind a local port.
