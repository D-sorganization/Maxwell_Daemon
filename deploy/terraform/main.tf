################################################################################
# Maxwell-Daemon Terraform module
# Provisions cloud VMs for the conductor fleet on AWS, GCP, or Azure.
# Cloud is selected via `var.cloud`.
################################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

##############################################################################
# SSH keypair (generated per deployment)
##############################################################################

resource "tls_private_key" "maxwell" {
  algorithm = "ED25519"
}

##############################################################################
# AWS
##############################################################################

resource "aws_key_pair" "maxwell" {
  count      = var.cloud == "aws" ? 1 : 0
  key_name   = "${var.name_prefix}-maxwell"
  public_key = tls_private_key.maxwell.public_key_openssh
}

resource "aws_security_group" "maxwell" {
  count       = var.cloud == "aws" ? 1 : 0
  name        = "${var.name_prefix}-maxwell"
  description = "Maxwell-Daemon conductor fleet"
  vpc_id      = var.aws_vpc_id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
    description = "SSH"
  }

  ingress {
    from_port   = var.conductor_port
    to_port     = var.conductor_port
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
    description = "Maxwell-Daemon API"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-maxwell" })
}

resource "aws_instance" "conductor" {
  count                  = var.cloud == "aws" ? 1 : 0
  ami                    = var.aws_ami
  instance_type          = var.aws_instance_type
  key_name               = aws_key_pair.maxwell[0].key_name
  vpc_security_group_ids = [aws_security_group.maxwell[0].id]
  subnet_id              = var.aws_subnet_id

  root_block_device {
    volume_size = var.disk_size_gb
    volume_type = "gp3"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-conductor", Role = "primary" })
}

resource "aws_instance" "agent" {
  count                  = var.cloud == "aws" ? var.agent_count : 0
  ami                    = var.aws_ami
  instance_type          = var.aws_instance_type
  key_name               = aws_key_pair.maxwell[0].key_name
  vpc_security_group_ids = [aws_security_group.maxwell[0].id]
  subnet_id              = var.aws_subnet_id

  root_block_device {
    volume_size = var.disk_size_gb
    volume_type = "gp3"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-agent-${count.index}", Role = "agent" })
}

##############################################################################
# GCP
##############################################################################

resource "google_compute_firewall" "maxwell" {
  count   = var.cloud == "gcp" ? 1 : 0
  name    = "${var.name_prefix}-maxwell"
  network = var.gcp_network

  allow {
    protocol = "tcp"
    ports    = ["22", tostring(var.conductor_port)]
  }

  source_ranges = var.allowed_cidr_blocks
  target_tags   = ["maxwell-daemon"]
}

resource "google_compute_instance" "conductor" {
  count        = var.cloud == "gcp" ? 1 : 0
  name         = "${var.name_prefix}-conductor"
  machine_type = var.gcp_machine_type
  zone         = var.gcp_zone
  tags         = ["maxwell-daemon"]

  boot_disk {
    initialize_params {
      image = var.gcp_image
      size  = var.disk_size_gb
    }
  }

  network_interface {
    network    = var.gcp_network
    subnetwork = var.gcp_subnetwork
    access_config {}
  }

  metadata = {
    ssh-keys = "ubuntu:${tls_private_key.maxwell.public_key_openssh}"
  }

  labels = merge(var.tags, { role = "primary" })
}

resource "google_compute_instance" "agent" {
  count        = var.cloud == "gcp" ? var.agent_count : 0
  name         = "${var.name_prefix}-agent-${count.index}"
  machine_type = var.gcp_machine_type
  zone         = var.gcp_zone
  tags         = ["maxwell-daemon"]

  boot_disk {
    initialize_params {
      image = var.gcp_image
      size  = var.disk_size_gb
    }
  }

  network_interface {
    network    = var.gcp_network
    subnetwork = var.gcp_subnetwork
    access_config {}
  }

  metadata = {
    ssh-keys = "ubuntu:${tls_private_key.maxwell.public_key_openssh}"
  }

  labels = merge(var.tags, { role = "agent" })
}

##############################################################################
# Azure
##############################################################################

resource "azurerm_resource_group" "maxwell" {
  count    = var.cloud == "azure" ? 1 : 0
  name     = "${var.name_prefix}-maxwell-rg"
  location = var.azure_location
  tags     = var.tags
}

resource "azurerm_network_security_group" "maxwell" {
  count               = var.cloud == "azure" ? 1 : 0
  name                = "${var.name_prefix}-maxwell-nsg"
  location            = azurerm_resource_group.maxwell[0].location
  resource_group_name = azurerm_resource_group.maxwell[0].name
  tags                = var.tags

  security_rule {
    name                       = "SSH"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = join(",", var.allowed_cidr_blocks)
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "Maxwell-API"
    priority                   = 1002
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = tostring(var.conductor_port)
    source_address_prefix      = join(",", var.allowed_cidr_blocks)
    destination_address_prefix = "*"
  }
}

resource "azurerm_linux_virtual_machine" "conductor" {
  count                           = var.cloud == "azure" ? 1 : 0
  name                            = "${var.name_prefix}-conductor"
  resource_group_name             = azurerm_resource_group.maxwell[0].name
  location                        = azurerm_resource_group.maxwell[0].location
  size                            = var.azure_vm_size
  admin_username                  = "ubuntu"
  disable_password_authentication = true
  tags                            = merge(var.tags, { role = "primary" })

  admin_ssh_key {
    username   = "ubuntu"
    public_key = tls_private_key.maxwell.public_key_openssh
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = var.disk_size_gb
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }

  network_interface_ids = [azurerm_network_interface.conductor[0].id]
}

resource "azurerm_network_interface" "conductor" {
  count               = var.cloud == "azure" ? 1 : 0
  name                = "${var.name_prefix}-conductor-nic"
  location            = azurerm_resource_group.maxwell[0].location
  resource_group_name = azurerm_resource_group.maxwell[0].name
  tags                = var.tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.azure_subnet_id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.conductor[0].id
  }
}

resource "azurerm_public_ip" "conductor" {
  count               = var.cloud == "azure" ? 1 : 0
  name                = "${var.name_prefix}-conductor-pip"
  resource_group_name = azurerm_resource_group.maxwell[0].name
  location            = azurerm_resource_group.maxwell[0].location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}
