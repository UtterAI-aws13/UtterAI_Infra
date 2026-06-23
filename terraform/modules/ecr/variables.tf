variable "repository_names" {
  type        = list(string)
  description = "생성할 ECR 레포지토리 이름 목록"
}

variable "image_tag_mutability" {
  type        = string
  description = "이미지 태그 변경 가능 여부. IMMUTABLE 권장 (동일 태그 덮어쓰기 방지)"
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability는 MUTABLE 또는 IMMUTABLE이어야 합니다."
  }
}
