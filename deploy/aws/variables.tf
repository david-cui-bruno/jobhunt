variable "aws_region" {
  description = "AWS region for the deployment."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used for AWS resource names and tags."
  type        = string
  default     = "jobhunt"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.project_name))
    error_message = "project_name must start with a letter and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "environment" {
  description = "Deployment environment tag."
  type        = string
  default     = "production"
}

variable "instance_type" {
  description = "EC2 instance type. t3a.medium is the conservative x86-64 default."
  type        = string
  default     = "t3a.medium"
}

variable "instance_architecture" {
  description = "AMI architecture. Use x86_64 with t3/t3a and arm64 with t4g."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.instance_architecture)
    error_message = "instance_architecture must be x86_64 or arm64."
  }
}

variable "root_volume_size_gb" {
  description = "Encrypted gp3 root volume size."
  type        = number
  default     = 40

  validation {
    condition     = var.root_volume_size_gb >= 30 && var.root_volume_size_gb <= 1024
    error_message = "root_volume_size_gb must be between 30 and 1024."
  }
}

variable "backup_retention_days" {
  description = "Number of days AWS Backup retains daily EC2 recovery points."
  type        = number
  default     = 14

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 365
    error_message = "backup_retention_days must be between 7 and 365."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention period."
  type        = number
  default     = 30
}

variable "monthly_budget_usd" {
  description = "Whole-account monthly AWS budget limit. Email notifications are added when alert_email is non-empty."
  type        = number
  default     = 50
}

variable "alert_email" {
  description = "Email for budget and CloudWatch alarm notifications. Leave empty until ready to subscribe."
  type        = string
  default     = ""

  validation {
    condition     = var.alert_email == "" || can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.alert_email))
    error_message = "alert_email must be empty or a valid email address."
  }
}

variable "enable_detailed_monitoring" {
  description = "Enable one-minute EC2 detailed monitoring. The CloudWatch Agent still emits memory and disk metrics."
  type        = bool
  default     = false
}

variable "enable_cpu_credit_alarm" {
  description = "Create a CPU credit alarm for T-family instances. Disable for non-burstable instance families."
  type        = bool
  default     = true
}
