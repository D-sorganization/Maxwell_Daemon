################################################################################
# Variables
################################################################################

variable "cloud" {
  description = "Target cloud provider: aws | gcp | azure"
  type        = string
  validation {
    condition     = contains(["aws", "gcp", "azure"], var.cloud)
    error_message = "cloud must be one of: aws, gcp, azure"
  }
}

variable "name_prefix" {
  description = "Prefix for all resource names"
  type        = string
  default     = "maxwell"
}

variable "agent_count" {
  description = "Number of agent worker VMs to provision"
  type        = number
  default     = 2
}

variable "conductor_port" {
  description = "TCP port the Maxwell-Daemon API listens on"
  type        = number
  default     = 8765
}

variable "disk_size_gb" {
  description = "Root disk size in GB"
  type        = number
  default     = 40
}

variable "allowed_cidr_blocks" {
  description = "CIDR ranges permitted to reach SSH and the API port"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags/labels to apply to all resources"
  type        = map(string)
  default = {
    managed-by = "terraform"
    project    = "maxwell-daemon"
  }
}

# ── AWS ───────────────────────────────────────────────────────────────────────

variable "aws_ami" {
  description = "AMI ID for conductor and agent instances (Ubuntu 22.04 recommended)"
  type        = string
  default     = "ami-0c02fb55956c7d316"  # us-east-1 Ubuntu 22.04 LTS
}

variable "aws_instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.medium"
}

variable "aws_vpc_id" {
  description = "VPC to place the security group in"
  type        = string
  default     = ""
}

variable "aws_subnet_id" {
  description = "Subnet to place instances in"
  type        = string
  default     = ""
}

# ── GCP ───────────────────────────────────────────────────────────────────────

variable "gcp_project" {
  description = "GCP project ID"
  type        = string
  default     = ""
}

variable "gcp_zone" {
  description = "GCP zone for instances"
  type        = string
  default     = "us-central1-a"
}

variable "gcp_machine_type" {
  description = "GCP machine type"
  type        = string
  default     = "e2-medium"
}

variable "gcp_image" {
  description = "GCP boot disk image"
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}

variable "gcp_network" {
  description = "GCP VPC network name"
  type        = string
  default     = "default"
}

variable "gcp_subnetwork" {
  description = "GCP subnetwork name (leave empty to use default)"
  type        = string
  default     = ""
}

# ── Azure ─────────────────────────────────────────────────────────────────────

variable "azure_location" {
  description = "Azure region"
  type        = string
  default     = "East US"
}

variable "azure_vm_size" {
  description = "Azure VM size"
  type        = string
  default     = "Standard_B2s"
}

variable "azure_subnet_id" {
  description = "Azure subnet resource ID"
  type        = string
  default     = ""
}
