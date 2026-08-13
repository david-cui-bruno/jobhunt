data "aws_iam_policy_document" "backup_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${var.project_name}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_assume_role.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

resource "aws_backup_vault" "jobhunt" {
  name = var.project_name
}

resource "aws_backup_plan" "daily" {
  name = "${var.project_name}-daily"

  rule {
    rule_name         = "daily-ec2"
    target_vault_name = aws_backup_vault.jobhunt.name
    schedule          = "cron(0 5 * * ? *)"
    start_window      = 60
    completion_window = 180

    lifecycle {
      delete_after = var.backup_retention_days
    }

    recovery_point_tags = local.common_tags
  }
}

resource "aws_backup_selection" "instance" {
  iam_role_arn = aws_iam_role.backup.arn
  name         = "${var.project_name}-instance"
  plan_id      = aws_backup_plan.daily.id
  resources    = [aws_instance.jobhunt.arn]
}
