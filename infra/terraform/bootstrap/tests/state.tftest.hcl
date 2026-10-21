mock_provider "aws" {}

variables {
  aws_account_id = "123456789012"
}

run "state_is_private_recoverable_and_encrypted" {
  command = plan
  assert {
    condition = (
      aws_s3_bucket.state.force_destroy == false &&
      aws_s3_bucket_public_access_block.state.block_public_acls &&
      aws_s3_bucket_public_access_block.state.block_public_policy &&
      aws_s3_bucket_public_access_block.state.ignore_public_acls &&
      aws_s3_bucket_public_access_block.state.restrict_public_buckets &&
      one(aws_s3_bucket_versioning.state.versioning_configuration).status == "Enabled" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms" &&
      aws_kms_key.state.enable_key_rotation
    )
    error_message = "Terraform state must be private, versioned, encrypted and protected against nonempty-bucket deletion."
  }
}

run "reject_invalid_account" {
  command = plan
  variables {
    aws_account_id = "not-an-account"
  }
  expect_failures = [var.aws_account_id]
}
