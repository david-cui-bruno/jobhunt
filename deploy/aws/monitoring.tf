resource "aws_cloudwatch_log_group" "system" {
  name              = "/${var.project_name}/system"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alerts" {
  count = var.alert_email == "" ? 0 : 1
  name  = "${var.project_name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "recover_system" {
  alarm_name          = "${var.project_name}-recover-system"
  alarm_description   = "Recover the EC2 instance after two failed system status checks."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = aws_instance.jobhunt.id
  }

  alarm_actions = concat(
    ["arn:aws:automate:${var.aws_region}:ec2:recover"],
    local.alert_topic_arns,
  )
  ok_actions = local.alert_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "instance_status" {
  alarm_name          = "${var.project_name}-instance-status"
  alarm_description   = "Notify when the operating system or EC2 host fails status checks."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = aws_instance.jobhunt.id
  }

  alarm_actions = local.alert_topic_arns
  ok_actions    = local.alert_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "high_cpu" {
  alarm_name          = "${var.project_name}-high-cpu"
  alarm_description   = "Notify when average CPU stays above 85 percent for 15 minutes."
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = aws_instance.jobhunt.id
  }

  alarm_actions = local.alert_topic_arns
  ok_actions    = local.alert_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "low_cpu_credit" {
  count               = var.enable_cpu_credit_alarm ? 1 : 0
  alarm_name          = "${var.project_name}-low-cpu-credit"
  alarm_description   = "Notify when a burstable instance has fewer than 24 CPU credits."
  namespace           = "AWS/EC2"
  metric_name         = "CPUCreditBalance"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 24
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = aws_instance.jobhunt.id
  }

  alarm_actions = local.alert_topic_arns
  ok_actions    = local.alert_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "high_memory" {
  alarm_name          = "${var.project_name}-high-memory"
  alarm_description   = "Notify when memory usage stays above 85 percent for 10 minutes."
  namespace           = "CWAgent"
  metric_name         = "mem_used_percent"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = aws_instance.jobhunt.id
  }

  alarm_actions = local.alert_topic_arns
  ok_actions    = local.alert_topic_arns
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-account-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = var.alert_email == "" ? {} : {
      forecasted = {
        threshold = 80
        type      = "FORECASTED"
      }
      actual = {
        threshold = 100
        type      = "ACTUAL"
      }
    }

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
