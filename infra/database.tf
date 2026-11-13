resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-database"
  subnet_ids = aws_subnet.database[*].id
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-database"
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"
  db_name        = "webhook_platform"
  username       = "webhook_admin"

  password_wo = var.database_password
  # Increment when deliberately changing the password; keep SSM in sync.
  password_wo_version = 1

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  multi_az               = false
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]

  backup_retention_period    = 1
  auto_minor_version_upgrade = true

  # Temporary demo: teardown deletes the database without retaining snapshots.
  deletion_protection      = false
  skip_final_snapshot      = true
  delete_automated_backups = true

  tags = { Name = "${var.project_name}-database" }
}
