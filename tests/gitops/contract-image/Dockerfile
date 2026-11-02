FROM python:3.12-alpine
RUN pip install --no-cache-dir boto3==1.43.18
WORKDIR /lab
CMD ["python3", "-u", "/lab/contract_worker.py"]
