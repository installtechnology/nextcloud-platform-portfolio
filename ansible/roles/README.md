# Roles — Fase 2

common prepara pacotes básicos, grupo e diretórios. docker instala Engine,
Buildx e Compose Plugin e valida daemon/plugin. Somente módulos builtins;
nenhum serviço da stack Nextcloud. Consultar ../README.md.

Fase 3: nextcloud_stack é usada apenas por deploy.yml.
Usa a collection community.docker fixada para pull/up/wait e builtins para
arquivos/probes; Compose tem fonte única em docker/. Não muda Terraform.
