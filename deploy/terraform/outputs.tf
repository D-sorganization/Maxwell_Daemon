################################################################################
# Outputs — fleet endpoint and SSH details
################################################################################

output "conductor_public_ip" {
  description = "Public IP of the primary conductor node"
  value = (
    var.cloud == "aws" ? (length(aws_instance.conductor) > 0 ? aws_instance.conductor[0].public_ip : "") :
    var.cloud == "gcp" ? (length(google_compute_instance.conductor) > 0 ? google_compute_instance.conductor[0].network_interface[0].access_config[0].nat_ip : "") :
    var.cloud == "azure" ? (length(azurerm_public_ip.conductor) > 0 ? azurerm_public_ip.conductor[0].ip_address : "") :
    ""
  )
}

output "fleet_api_endpoint" {
  description = "Maxwell-Daemon API base URL"
  value       = "http://${local.conductor_ip}:${var.conductor_port}/api/v1"
}

output "agent_ips" {
  description = "Public IPs of agent worker nodes"
  value = (
    var.cloud == "aws" ? aws_instance.agent[*].public_ip :
    var.cloud == "gcp" ? [for inst in google_compute_instance.agent : inst.network_interface[0].access_config[0].nat_ip] :
    []
  )
}

output "ssh_private_key" {
  description = "Private SSH key (PEM) — store securely, do not commit"
  value       = tls_private_key.maxwell.private_key_openssh
  sensitive   = true
}

output "ssh_config" {
  description = "Ready-to-use ~/.ssh/config block for the provisioned fleet"
  value = <<-EOT
    Host ${var.name_prefix}-conductor
      HostName ${local.conductor_ip}
      User ubuntu
      IdentityFile ~/.ssh/${var.name_prefix}-maxwell.pem

    %{for i, ip in local.agent_ips~}
    Host ${var.name_prefix}-agent-${i}
      HostName ${ip}
      User ubuntu
      IdentityFile ~/.ssh/${var.name_prefix}-maxwell.pem
    %{endfor~}
  EOT
}

################################################################################
# Locals used across outputs
################################################################################

locals {
  conductor_ip = (
    var.cloud == "aws" ? (length(aws_instance.conductor) > 0 ? aws_instance.conductor[0].public_ip : "0.0.0.0") :
    var.cloud == "gcp" ? (length(google_compute_instance.conductor) > 0 ? google_compute_instance.conductor[0].network_interface[0].access_config[0].nat_ip : "0.0.0.0") :
    var.cloud == "azure" ? (length(azurerm_public_ip.conductor) > 0 ? azurerm_public_ip.conductor[0].ip_address : "0.0.0.0") :
    "0.0.0.0"
  )

  agent_ips = (
    var.cloud == "aws" ? aws_instance.agent[*].public_ip :
    var.cloud == "gcp" ? [for inst in google_compute_instance.agent : inst.network_interface[0].access_config[0].nat_ip] :
    []
  )
}
