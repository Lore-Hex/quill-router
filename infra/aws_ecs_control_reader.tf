# Resolve metadata only; the password must never enter Terraform state.
data "aws_secretsmanager_secret" "tr_eu_clickhouse_control_reader" {
  name = "quill/tr-eu-clickhouse-read-password"
}

# The existing tr-cp-exec role and its other secret grants remain externally managed.
resource "aws_iam_role_policy" "tr_cp_clickhouse_control_reader" {
  role = "tr-cp-exec"
  name = "tr-cp-clickhouse-control-reader"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = data.aws_secretsmanager_secret.tr_eu_clickhouse_control_reader.arn
    }]
  })
}
