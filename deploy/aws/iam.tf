data "aws_iam_policy_document" "instance_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.project_name}-instance"
  assume_role_policy = data.aws_iam_policy_document.instance_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

data "aws_iam_policy_document" "instance_state" {
  statement {
    sid       = "ListJobhuntPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["bootstrap/*", "backups/*"]
    }
  }

  statement {
    sid = "ManageJobhuntStateObjects"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.state.arn}/bootstrap/*",
      "${aws_s3_bucket.state.arn}/backups/*",
    ]
  }

  statement {
    sid     = "ReadJobhuntParameters"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project_name}/anthropic_api_key",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project_name}/kith_env",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project_name}/application_answers",
    ]
  }

  statement {
    sid       = "EmitJobhuntMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Jobhunt"]
    }
  }
}

resource "aws_iam_role_policy" "instance_state" {
  name   = "${var.project_name}-state"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance_state.json
}

resource "aws_iam_instance_profile" "jobhunt" {
  name = "${var.project_name}-instance"
  role = aws_iam_role.instance.name
}
