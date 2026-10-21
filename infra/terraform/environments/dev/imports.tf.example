# Copy to imports.tf only if this exact ECR repository already exists in the
# target account/region. Import adopts it; inspect plan for replacements first.
import {
  to = aws_ecr_repository.services["service-template"]
  id = "lostlink/service-template"
}
