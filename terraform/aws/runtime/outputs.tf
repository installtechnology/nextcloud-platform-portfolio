output "instance_id" {
  value = aws_instance.host.id
}
output "public_ip" {
  description = "IPv4 dinâmico; consultar novamente após stop/start ou recriação."
  value       = aws_instance.host.public_ip
}
output "nextcloud_url" {
  description = "URL futura: nesta fase não existe aplicação instalada."
  value       = "http://${aws_instance.host.public_ip}"
}
output "ssh_user" {
  value = "ubuntu"
}
output "vpc_id" {
  value = aws_vpc.main.id
}
output "data_bucket_name" {
  value = var.data_bucket_name
}

output "security_group_id" { value = aws_security_group.host.id }
output "instance_type" { value = var.instance_type }
output "root_volume_size_gib" { value = var.root_volume_size_gib }
