data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  ubuntu_arch = var.instance_architecture == "arm64" ? "arm64" : "amd64"

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
    Application = "jobhunt"
  }

  backup_bucket_name = "${var.project_name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  alert_topic_arns   = var.alert_email == "" ? [] : [aws_sns_topic.alerts[0].arn]
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${local.ubuntu_arch}-server-*"]
  }

  filter {
    name   = "architecture"
    values = [var.instance_architecture]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_vpc" "jobhunt" {
  cidr_block           = "10.47.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

resource "aws_internet_gateway" "jobhunt" {
  vpc_id = aws_vpc.jobhunt.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.jobhunt.id
  cidr_block              = "10.47.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.project_name}-public-${data.aws_availability_zones.available.names[0]}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.jobhunt.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.jobhunt.id
  }

  tags = {
    Name = "${var.project_name}-public"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "instance" {
  name        = "${var.project_name}-instance"
  description = "No inbound access; outbound HTTPS and package traffic only"
  vpc_id      = aws_vpc.jobhunt.id

  # There are intentionally no ingress rules. Administration uses SSM.
  egress {
    description = "Outbound internet access for package repositories, APIs, Gmail, and ATS sites"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-instance"
  }
}

resource "aws_instance" "jobhunt" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.jobhunt.name
  # A temporary public address gives cloud-init immediate package access. The
  # Elastic IP below replaces it as soon as the instance becomes available.
  associate_public_ip_address = true
  monitoring                  = var.enable_detailed_monitoring

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    aws_region          = var.aws_region
    cloudwatch_log_name = aws_cloudwatch_log_group.system.name
    agent_arch          = var.instance_architecture == "arm64" ? "arm64" : "amd64"
  })

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-root"
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  credit_specification {
    cpu_credits = "unlimited"
  }

  tags = {
    Name   = var.project_name
    Backup = "daily"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [ami]

    precondition {
      condition = (
        var.instance_architecture == "x86_64" && (
          startswith(var.instance_type, "t3.") || startswith(var.instance_type, "t3a.")
        )
        ) || (
        var.instance_architecture == "arm64" && startswith(var.instance_type, "t4g")
      )
      error_message = "This first deployment supports x86_64 t3/t3a or arm64 t4g instances. Change instance_type and instance_architecture together."
    }
  }

  depends_on = [aws_route_table_association.public]
}

resource "aws_eip" "jobhunt" {
  domain = "vpc"

  tags = {
    Name = "${var.project_name}-egress"
  }

  depends_on = [aws_internet_gateway.jobhunt]
}

resource "aws_eip_association" "jobhunt" {
  allocation_id = aws_eip.jobhunt.id
  instance_id   = aws_instance.jobhunt.id
}

resource "aws_s3_bucket" "state" {
  bucket        = local.backup_bucket_name
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-bootstrap-artifacts"
    status = "Enabled"

    filter {
      prefix = "bootstrap/"
    }

    expiration {
      days = 2
    }

    noncurrent_version_expiration {
      noncurrent_days = 2
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "retain-backup-history"
    status = "Enabled"

    filter {
      prefix = "backups/"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}
