FROM python:3.12-alpine@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d
WORKDIR /app
COPY policy.py /app/policy.py
USER 65532:65532
EXPOSE 8443
CMD ["python", "-u", "policy.py"]
