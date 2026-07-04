# gpu-inference-queue에서 애플리케이션(ml-gpu-worker)이 아닌 정체불명의 소비자가
# ReceiveMessage/DeleteMessage를 호출해 메시지를 실제 처리 전에 가로채는 정황이
# 발견되어, 호출자(IAM principal/source IP) 특정을 위해 SQS data event 전용
# CloudTrail을 추가한다. 자세한 경위는 트러블슈팅 로그 참고.
module "cloudtrail" {
  source = "../../../modules/cloudtrail"

  project_name   = var.project_name
  environment    = var.environment
  aws_account_id = data.aws_caller_identity.current.account_id
  aws_region     = var.aws_region

  sqs_data_event_queue_arns = [
    module.sqs.gpu_inference_queue_arn,
  ]
}
