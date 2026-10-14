# Scripts

ci/deploy.py orquestra operações do runner: prepara inputs/backend, valida conta/
região, plan/apply somente por modo explícito, obtém outputs públicos, administra
SSH temporário e verifica host key. Não duplica instalação/configuração Ansible.
Importar o módulo não executa operações. Testes offline em ci/tests/.

Instalar `python -m pip install -r scripts/ci/tests/requirements.txt` no ambiente
de desenvolvimento para check_static.py/check_iam.py; test_deploy.py usa somente
biblioteca padrão. Os testes não autenticam em AWS nem executam SSH/Terraform real.

O [validador offline do PR2](backup/README.md) verifica contratos e integridade
de arquivos locais, sem criar backup ou comprovar restauração funcional.
O [controlador de captura do PR3](backup/capture/README.md) aceita somente fixture
Docker local isolada e produz candidatos incompletos. Uso operacional bloqueado.
Sem scripts operacionais de backup, destroy, DR ou Azure. Estado/planos/chaves
temporários ficam em RUNNER_TEMP, nunca artifacts ou repositório. Veja
[workflows](../.github/workflows/README.md) e [Fase 6](../docs/phase-6.md).
