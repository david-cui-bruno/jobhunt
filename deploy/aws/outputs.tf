output "instance_id" {
  description = "EC2 instance ID used by SSM Session Manager and bootstrap scripts."
  value       = aws_instance.jobhunt.id
}

output "public_ip" {
  description = "Stable Elastic IP used for outbound ATS traffic."
  value       = aws_eip.jobhunt.public_ip
}

output "state_bucket" {
  description = "Private versioned bucket for bootstrap artifacts and application backups."
  value       = aws_s3_bucket.state.id
}

output "aws_region" {
  description = "AWS region used by the deployment helper scripts."
  value       = var.aws_region
}

output "anthropic_parameter_name" {
  description = "SecureString parameter populated by stage-and-bootstrap.sh, not Terraform state."
  value       = "/${var.project_name}/anthropic_api_key"
}

output "application_answers_parameter_name" {
  description = "SecureString containing approved personal application answers, populated outside Terraform state."
  value       = "/${var.project_name}/application_answers"
}

output "backup_vault" {
  description = "AWS Backup vault containing daily EC2 recovery points."
  value       = aws_backup_vault.jobhunt.name
}

output "ssm_shell_command" {
  description = "Open a shell without exposing SSH."
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.jobhunt.id}"
}

output "next_step" {
  description = "Safe next command after apply."
  value       = "CONFIRM_LOCAL_WORKERS_STOPPED=yes ANTHROPIC_API_KEY=... ./stage-and-bootstrap.sh"
}
