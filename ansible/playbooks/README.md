# Playbooks — Fase 2

bootstrap.yml configura somente Ubuntu e Docker via SSH/become, com grupo
platform. Não provisiona cloud nem instala aplicação. Executar a partir de
ansible/ com ansible.cfg explícito; consultar ../README.md.

deploy.yml importa bootstrap.yml e executa nextcloud_stack em play separado da
Fase 3. Bootstrap permanece só common/docker, sem depender da nova collection.
Instalar requirements.yml antes do syntax-check de deploy. Credenciais vêm de
env/Vault, não CLI/Git.

prepare-capture-sandbox.yml prepara somente arquivos/staging para ensaio Linux
local do PR3, em grupo capture_sandbox e projeto nc-capture-test-*. Não instala
ferramentas, inicia serviços ou executa captura. Não é importado pelo deploy.
Validações POSIX e integração real permanecem pendentes antes de uso operacional.
