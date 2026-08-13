mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/terraform-test"
      user_id    = "AIDATEST"
    }
  }

  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b"]
    }
  }

  mock_data "aws_ami" {
    defaults = {
      id           = "ami-0123456789abcdef0"
      architecture = "x86_64"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Resource\":\"*\"}]}"
    }
  }
}

run "safe_defaults" {
  command = plan

  assert {
    condition     = aws_instance.jobhunt.root_block_device[0].encrypted
    error_message = "The EC2 root volume must be encrypted."
  }

  assert {
    condition     = aws_instance.jobhunt.root_block_device[0].volume_type == "gp3"
    error_message = "The EC2 root volume must use gp3 storage."
  }

  assert {
    condition     = aws_instance.jobhunt.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 must be required."
  }

  assert {
    condition     = aws_eip.jobhunt.domain == "vpc"
    error_message = "The deployment must use a stable VPC Elastic IP."
  }

  assert {
    condition     = aws_instance.jobhunt.associate_public_ip_address
    error_message = "The instance needs temporary first-boot internet access before the Elastic IP association completes."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.state.block_public_acls &&
      aws_s3_bucket_public_access_block.state.block_public_policy &&
      aws_s3_bucket_public_access_block.state.ignore_public_acls &&
      aws_s3_bucket_public_access_block.state.restrict_public_buckets
    )
    error_message = "The state bucket must block every form of public access."
  }

  assert {
    condition = length([
      for rule in aws_backup_plan.daily.rule : rule
      if length([
        for lifecycle in rule.lifecycle : lifecycle
        if lifecycle.delete_after == 14
      ]) == 1
    ]) == 1
    error_message = "The default backup retention must be 14 days."
  }

  assert {
    condition     = aws_instance.jobhunt.instance_type == "t3a.medium"
    error_message = "The conservative initial instance must be t3a.medium."
  }
}

run "reject_architecture_mismatch" {
  command = plan

  variables {
    instance_type         = "t4g.medium"
    instance_architecture = "x86_64"
  }

  expect_failures = [aws_instance.jobhunt]
}

run "allow_arm_pair" {
  command = plan

  variables {
    instance_type         = "t4g.medium"
    instance_architecture = "arm64"
  }

  assert {
    condition     = aws_instance.jobhunt.instance_type == "t4g.medium"
    error_message = "A matching t4g/arm64 pair should be accepted."
  }
}
