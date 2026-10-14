# Instalação inicial e retomada segura — revisão operacional

A correção foi publicada e validada no deploy real: Nextcloud instalado e healthy, sem reinstalação no rerun posterior do Grafana. As seções de diagnóstico/testes abaixo registram o estado anterior à correção e suas fixtures isoladas. O resultado atual está na [Fase 6](phase-6.md).

## Causa confirmada

A imagem fixada nextcloud:34.0.4-apache, digest sha256:72f5cc3347042dd5dcad72edc376b19d0e042194af344a549820affd04804c8f, sincroniza os arquivos e version.php antes de executar o hook pre-installation e maintenance:install. Esse hook abortou a primeira tentativa devido ao problema de carregamento do guard, já corrigido.

Nas retomadas, o entrypoint calcula installed_version a partir de version.php, não do flag installed em config.php. Com a mesma versão já copiada, não volta ao bloco de instalação automática. O hook before-starting antigo apenas validava storage e copiava configuração: não instalava a aplicação. O Ansible não tinha tarefa posterior de instalação; Compose wait exigia um healthcheck com installed=true. Esperar mais tempo não resolve esse estado.

Os inputs NEXTCLOUD_ADMIN_USER, NEXTCLOUD_ADMIN_PASSWORD_FILE e POSTGRES_* estavam presentes no container. Os dois secrets eram legíveis por www-data. Não houve falta de senha. A falha original aconteceu antes da chamada do instalador oficial.

O entrypoint e o comando foram inspecionados diretamente na imagem fixada. Também foi revisado o [comando oficial 34.0.4](https://raw.githubusercontent.com/nextcloud/server/v34.0.4/core/Command/Maintenance/Install.php). A validação de contrato usa as classes da própria imagem, sem executar instalação.

## Estado encontrado somente por leitura

- Nextcloud: installed=false, Apache ativo, configuração S3 e marcador carregados.
- PostgreSQL: banco nextcloud com zero relações, funções de aplicação, domains/enums e schemas adicionais. Portanto não há esquema Nextcloud nem tabelas com registros de tentativa anterior.
- S3: ListObjectsV2 com MaxKeys=1 encontrou zero objetos ativos, via Instance Role; não houve Put/Delete.
- Diretório data: zero entradas além dos arquivos auxiliares permitidos.
- PostgreSQL e Redis healthy; infraestrutura e volumes preservados.

São observações pontuais. O instalador repete as verificações no momento da retomada. Não inferir que outro cliente/banco/ambiente também está vazio. A consulta S3 não enumera versões históricas/delete markers: elas não são excluídas nem adotadas. Se houver evidência de instalação/dados anteriores, suspender a instalação e investigar sua origem.

## Fluxo reutilizável

1. O entrypoint oficial continua responsável por sincronizar os arquivos da imagem e por seu fluxo de upgrade. A inicialização desta plataforma usa exclusivamente before-starting, depois da sincronização e antes do Apache.
2. configure-nextcloud.sh executa o storage guard existente e copia zz-platform.config.php antes de chamar install-nextcloud.php.
3. O instalador obtém lock exclusivo em config/.platform-install.lock e valida novamente o guard. Carrega o contexto CLI oficial e lê o estado efetivo sem confiar em version.php como indicação de instalação.
4. installed=true com identidade S3 válida retorna ALREADY_INSTALLED: nenhum comando de instalação, leitura das senhas administrativas, limpeza ou rotação.
5. installed=false exige parâmetros existentes compatíveis, diretório local sem user/app data, banco dedicado sem objetos de aplicação e bucket sem objetos ativos. Consultas ao catálogo usam BEGIN READ ONLY. O lock serializa os instaladores desta plataforma que compartilham o volume; não autoriza instalações manuais concorrentes.
6. Somente depois dessas verificações chama a classe oficial OC\\Core\\Command\\Maintenance\\Install, correspondente a maintenance:install. Symfony ArrayInput contém as opções exclusivamente em memória; não chama occ com senhas em argv. Exige retorno zero e confirmação installed=true com o mesmo storage.
7. Apache inicia após o hook. Compose wait e os probes Ansible continuam exigindo aplicação instalada, sem maintenance/upgrade pendente. O healthcheck nunca considera uma instância não instalada como healthy.

Em volume vazio, o entrypoint copia os arquivos e o hook instala. Em cópia interrompida com version.php presente e banco/bucket vazios, o mesmo hook retoma a instalação. Em deploy instalado, só valida e pula a instalação. Não há dependência circular de healthcheck: a instalação acontece antes dele, não depois do Compose wait.

Compose deixa de fornecer ao entrypoint NEXTCLOUD_ADMIN_USER/PASSWORD_FILE e POSTGRES_PASSWORD_FILE; usa PLATFORM_ADMIN_USER, PLATFORM_ADMIN_PASSWORD_FILE e PLATFORM_POSTGRES_PASSWORD_FILE. Isso desativa o auto-install nativo que executaria senhas em argumentos e limita o instalador a um único caminho. As entradas da .env, Variables e seis GitHub Secrets existentes permanecem iguais; não são necessários novos secrets.

Ansible copia o novo arquivo do diretório docker do checkout. A alteração de env/mounts faz recreate=auto atualizar somente o container Nextcloud neste patch; volumes nomeados e dependências não são removidos. Rerun sem mudanças não força recriação. Não adicionar restart ou instalação manual exclusivos da EC2.

## Falhas e proteção de dados

Reason codes fixos: STORAGE_GUARD_FAILED, STORAGE_CONFIG_MISMATCH, INSTALL_CONFIG_MISMATCH, LOCAL_DATA_NOT_EMPTY, DATABASE_NOT_EMPTY, DATA_BUCKET_NOT_EMPTY, INSTALL_INPUT_INVALID, SECRET_INPUT_INVALID, INSTALL_LOCK_ERROR, INSTALL_FAILED, INSTALL_NOT_CONFIRMED e INITIAL_INSTALL_ERROR. Mensagens do driver, stdout arbitrário e traces não são emitidos. Warnings não suprimidos abortam; a supressão @ de operações opcionais do código oficial é preservada. exit/die inesperado não pode transformar falha em sucesso.

O comando recebe secrets dos arquivos montados apenas em memória, com output NullOutput e entrada não interativa. Não gera shell/argv com senhas, arquivo de opções, log ou artifact. config.php continua sendo configuração operacional privada do Nextcloud, que naturalmente contém sua senha de banco; nunca deve ser impresso, enviado como artifact ou versionado.

Há uma tentativa por invocação. Se a instalação escrever esquema/objetos e falhar, uma reinicialização não reinstala sobre eles: as verificações bloqueiam a operação. Preservar banco, volume/config, marcador e objetos; diagnosticar versão, consistência e estágio da tentativa e propor recuperação específica sem limpeza. Não completar installed=true manualmente, não usar migrations/reset como atalho e não alterar storage guard. Não há rollback destrutivo automático.

## Testes e limites

Comandos locais:

```powershell
python docker/tests/test_initial_installation.py .validation-tools/php/php.exe
# Com engine Docker Linux e imagem fixada já disponível, somente fixtures isoladas:
python docker/tests/test_entrypoint_sequence.py
$env:STORAGE_GUARD_OFFICIAL_CONFIGS = (Resolve-Path .validation-tools/nextcloud-34.0.4-source).Path
python docker/tests/test_storage_guard.py .validation-tools/php/php.exe
python docker/tests/check_storage.py .validation-tools/php/php.exe
Get-ChildItem docker/*.php | ForEach-Object { & .validation-tools/php/php.exe -n -l $_.FullName }
.validation-tools/php/php.exe -n -l docker/tests/check_install_command.php
$env:PYTHONPATH = (Resolve-Path .validation-libs).Path
python ansible/tests/check_static.py
python docker/tests/check_static.py --docker-cli .validation-tools/docker.exe --docker-config .validation-tools/config
python -m unittest discover -s scripts/ci/tests -p 'test_*.py'
python scripts/ci/tests/check_iam.py
git diff --check
```

- 18 testes do fluxo PHP: novo, interrompido, segundo run, ordem S3/guard/preflight/instalação, DB/S3/local/config incompatíveis (inclusive após instalação), healthcheck antes/depois, output/exception/warnings sanitizados, exit(0) recusado e arquivos auxiliares que não podem ocultar diretórios de dados. Incluem dois processos concorrentes com uma única instalação e retorno não zero mesmo quando o comando simulado já escreveu installed=true, sem anúncio de sucesso.
- 17 regressões do guard, incluindo os nove configs oficiais; check_storage.py aprovado.
- Lint dos cinco PHP da aplicação e do contrato PHP aprovado.
- Os mesmos 18+17 testes passaram na imagem fixada, PHP 8.5.11. check_install_command.php validou definição, binding e validateInput da classe oficial com ArrayInput/NullOutput; execute/install não foi chamado. Nenhum volume operacional foi montado, nenhuma rede/S3/DB real acessada pelos testes.
- Dois testes executam o entrypoint original, sem alterações, na mesma imagem: volume novo seguido de reinicialização instalada, e retomada com version.php/installed=false. Usam o hook/guard reais, mas adapters simulados para instalação, banco e S3. Apache é substituído por um registro de fase; um interceptor recusa qualquer chamada nativa de install/upgrade. Rede bloqueada e somente diretórios temporários de teste montados.
- Contratos Ansible (15 YAML/Jinja, 59 tasks), Docker/S3/Compose CLI com rejeição de inputs vazios, dez testes CI e contrato IAM estático aprovados.
- O contrato CI check_static.py diretamente no checkout operacional recusou backend.tf ativados e ignorados. Passou numa cópia dos arquivos versionados, sem esses artefatos operacionais. Backends não foram removidos ou alterados.
- Gitleaks sem secrets. git diff --check aprovado.

Testes do fluxo usam adapters controlados para simular banco, bucket e comando, exercitando o código PHP real da orquestração. Não representam uma instalação completa com PostgreSQL/S3 reais. Integração final, renovação IMDS e duas execuções Ansible operacionais permanecem pendentes da autorização. Ansible syntax-check/lint local não disponível no Windows; nenhum playbook foi executado na EC2 nesta etapa.

## Auditoria final do entrypoint e dos locks

Na imagem exata, /entrypoint.sh chama maintenance:install na linha 260, antes de before-starting na linha 327. SHA-256 do script inspecionado: c1c5ad59db5811b4e9fc656eb0bfae4b1418ddf3f5fd6492a02212532fbd259e. Portanto só mover o instalador para esse hook seria insuficiente se os inputs oficiais continuassem presentes. O Compose local não fornece NEXTCLOUD_ADMIN_USER, NEXTCLOUD_ADMIN_PASSWORD, NEXTCLOUD_ADMIN_PASSWORD_FILE, POSTGRES_PASSWORD ou POSTGRES_PASSWORD_FILE ao serviço Nextcloud; nenhum desses nomes existe nos defaults da imagem. Não há env_file no serviço. A substituição de NEXTCLOUD_ADMIN_USER da .env preenche apenas PLATFORM_ADMIN_USER, sem exportar automaticamente o nome original para o container.

file_env não cria uma variável ausente sem valor/default. Com os aliases PLATFORM_*, o teste do entrypoint para ambos os inputs oficiais de administrador é falso, mesmo em volume vazio. O ramo nativo não instala; o hook é o único caminho de instalação desta plataforma. A mensagem oficial sugerindo instalação via web pode aparecer antes do hook em um volume novo, mas o Apache ainda não foi iniciado.

Sequência observada nos testes da imagem:

```text
Novo: sync de arquivos/version.php -> auto-install nativo desativado
      -> liberação de nextcloud-init-sync.lock
      -> guard -> zz-platform.config.php S3
      -> lock da plataforma -> guard -> estado -> checks local/DB/S3
      -> instalação simulada -> confirmação -> fase Apache simulada
Interrompido: sem repetir sync/instalação nativa -> mesma sequência do hook
Instalado: lock -> guard -> estado válido -> ALREADY_INSTALLED -> fase Apache simulada
```

O teste adquire o lock oficial de sincronização com LOCK_EX|LOCK_NB dentro do callback do guard, enquanto o lock da plataforma está ativo: a aquisição passa, comprovando que não existe dependência circular entre esses dois locks. Outro teste inicia dois processos com o mesmo lock da plataforma; somente um executa install, e o segundo relê o estado depois de adquirir o lock e retorna ALREADY_INSTALLED. O guard filho usa locks de leitura de config, liberados antes do comando oficial; não tenta adquirir o lock da plataforma. Isso verifica ausência de deadlock nesse encadeamento, não garante disponibilidade de serviços externos se eles travarem.

.platform-storage.json registra exclusivamente identidade de storage, antes da instalação, e não comprova sucesso. .platform-install.lock também não é marcador de sucesso. INSTALLED somente é emitido após retorno zero e releitura de installed=true/S3 correspondente; o wrapper nunca define installed=true manualmente em produção. Mesmo se o comando escrever esse flag e retornar erro, a execução falha sem anunciar sucesso. Falha tardia do código oficial requer diagnóstico do estado preservado; não se deve inferir instalação íntegra apenas de um flag nem reinstalar ou limpar automaticamente.

Nesta auditoria não foi necessária nova alteração de produção: apenas testes de regressão e este registro foram acrescentados. Os testes isolados não instalaram a aplicação real nem usaram volumes, banco, buckets ou secrets operacionais. Não houve alteração de Grafana.

## Grafana — investigação separada

O container responde pela bridge observability: HTTP 200, database=ok. HostConfig.PortBindings solicita 127.0.0.1:3000, mas NetworkSettings.Ports tem 3000/tcp=null; não há listener host nem regra NAT dessa porta. A conexão localhost falha antes de alcançar Grafana, não por sua senha ou banco.

Grafana está conectado somente à rede observability internal. Os sintomas correspondem ao [relato primário no Moby de publicação não ativada em bridge exclusivamente internal](https://github.com/moby/moby/discussions/53256). Isso indica a configuração de rede como causa provável; nenhuma reprodução com mudança de rede foi feita, pois a investigação foi somente leitura.

Nenhuma alteração Grafana está incluída. A revisão posterior resolveu essa falha com bridge dedicada e publicação IPv4 TCP/3000 restrita a CIDRs administrativos. A orientação loopback/túnel desta investigação foi substituída pelo [acesso Grafana aplicado](grafana-browser-access.md); coletores permanecem sem portas externas.

## Retomada futura — somente após revisão/autorização

1. Revisar os diffs, publicar a correção somente quando autorizado e repetir a conferência dos states/plans 0/0/0 e do IP/CIDRs.
2. Repetir os checks de banco/bucket/dados locais antes da retomada. Qualquer mudança suspende a instalação e exige análise não destrutiva.
3. Executar o workflow autorizado no código publicado, sem patch manual na EC2. Ansible copia o instalador e Compose recria apenas Nextcloud conforme env/mounts alterados, com os volumes atuais.
4. Conferir INSTALLED ou ALREADY_INSTALLED, occ status, healthchecks e integração PostgreSQL/Redis/S3. Confirmar config.php/marcador e classe/bucket/região sem mostrar secrets.
5. Validar HTTP e métricas. Um futuro teste de upload/download exige autorização para dados de teste. Se aparecer falha, preservar tudo e parar; não repetir instalação em DB parcial.

Nenhuma Fase 7, backup ou DR foi iniciada.
