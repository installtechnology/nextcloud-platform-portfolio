# Fase 7 — PR3: captura local consistente, implementação isolada

Base: PR2 publicado, checkout inicial limpo em `9df3b61`. Contratos do PR1 e
validador offline permanecem inalterados. Não houve acesso à EC2, AWS/S3,
manutenção/dump reais, instalação de dependências, deploy ou alterações operacionais.

## Fatos examinados no código

- Compose fixa Nextcloud **34.0.4-apache** e PostgreSQL **17.11-bookworm**, ambos por
  digest. Redis usa 7.4.8. Não foi consultado o ambiente operacional.
- `nextcloud_html` monta `/var/www/html`; `postgres_data` monta PGDATA. Os nomes
  efetivos prefixados pelo Compose não são presumidos: o adaptador descobre mounts
  e IDs por labels/inspect e registra a identidade exata da fixture.
- Ansible mantém secrets como arquivos e `.env` separado, recusa rotação acidental
  e publica hooks do repositório. O guard mantém identidade S3 sem static keys.
- O instalador idempotente retorna ALREADY_INSTALLED quando installed=true; o
  entrypoint oficial em cache executa before-starting nas reinicializações.
- O healthcheck atual fica negativo durante manutenção. Não usar Compose wait
  como gate da captura. Não há serviço cron dedicado no Compose atual, o que
  não prova ausência de tarefas externas ou execs manuais no host.

## Arquitetura e limites de escopo

O novo pacote `scripts/backup/capture` separa controlador, staging/journal,
transporte Linux, adaptador Docker isolado, archives e leitor PHP. A CLI offline
do PR2 continua independente. Não há import automático da captura pelo deploy.

O engine escreve intenções antes de efeitos, mantém estado anterior, fingerprint
de manutenção e IDs. Lock local bloqueia concorrência. SIGINT/SIGTERM fazem
cleanup limitado por timeouts; SIGKILL/reboot exigem recovery do journal e
supervisor externo. Reserva local ajuda a registrar ENOSPC; não garante recuperação
de um filesystem indisponível/corrompido ou de journal pending ambíguo.

Somente uma fixture Linux/root de **três serviços** rotulados, redes internas,
sem portas publicadas, volumes privados e binds sintéticos pode ser usada pelo
adaptador atual. Projetos operacionais são rejeitados antes de executar Docker.
Isso é uma barreira de escopo, não autenticação contra um administrador root.

Quiescência exige manutenção, parada graciosa do container, exit zero sem OOM/
SIGKILL, nenhuma sessão par no DB e ausência de consumidores externos dos volumes/
redes. StopSignal WINCH e Cmd Apache são conferidos, não presumidos. A eficácia
real da drenagem precisa ser medida no ensaio. Host cron, operadores, deploy e
escritores S3 necessitam fencing próprio; maintenance mode não prova exclusão.

Artefatos: dump custom com ownership/ACL/criação do DB; todo config/ e HTML,
sem excluir dados mutáveis; parâmetros com SQL global sem passwords, owner,
collation/extensões, secrets protegidos, env e código runtime. Não capturar PGDATA
em execução, cache Redis, volumes de observabilidade ou Terraform state.
Não há invenção de VersionIds, inventário S3 ou hash ciphertext.

O resultado é **local_candidate_only**, não manifesto completo. Não gera
object_inventory, completion marker, criptografia, transferência, autorização de
Destroy ou evidência funcional de restore. Captura interrompida é abandonada;
recovery só restaura serviços quando ownership e identidade forem confirmados.

## Matriz de validação

| Executado/pendente | Evidência ou limitação |
| --- | --- |
| Windows: engine com mocks e arquivos temporários | Estados, concorrência, ownership, falhas, cancelamento e journal; não prova consistência operacional |
| PR3 Windows | 74 testes: 71 aprovados e 3 skips POSIX; inclui PHP real, encerramento abrupto de filho sintético e adaptador Docker com mocks |
| Windows: PHP real + OC Config oficial já em cache | Configurações sintéticas, apps.config.php, precedência e supressão de valores; não é execução da imagem Docker |
| PR2 Windows reexecutado | 187 aprovados, 2 skips POSIX; uso operacional Linux continua bloqueado |
| Linux POSIX | Pendente: sem distribuição WSL instalada e sem Docker disponível |
| PostgreSQL 17 e Nextcloud 34 reais | Pendente: pg_dump/pg_restore catálogo, globals/roles, consistência e manutenção reais |
| Supervisor systemd | Template inativo; kill/reboot/ExecStopPost/timeouts não validados operacionalmente |
| Ansible | YAML e caminhos de fontes conferidos com PyYAML em cache; syntax-check/lint e execução não realizados |

Resultados finais e contagens constam no relatório de entrega. Nenhum mock,
hash local ou catálogo de dump constitui prova de restore funcional.

Regressões executadas no Windows: PR2 (189 testes, 187 aprovados e 2 skips
POSIX), contratos originais (73 aprovados), deploy offline (13 aprovados),
storage guard (17 aprovados, incluindo nove configs oficiais em cache) e instalação inicial (18
aprovados). Lint PHP, AST Python, parsing YAML, links locais, scan de secrets
Gitleaks e verificações de whitespace passaram. O ensaio Docker do entrypoint
não foi executado porque não há Docker disponível. Não houve instalação de
dependências. HEAD e referência local origin/main permaneceram em `9df3b61`;
nenhuma consulta remota foi necessária nesta tarefa.

## Auditoria final de segurança e falhas

Revisados os 23 arquivos, inclusive os 19 untracked. Correções locais comprovadas:

- Dependências e disponibilidade PostgreSQL conferidas **antes** de desligar
  manutenção; manutenção preexistente não é regravada nem desligada.
- Journal interno inválido (tipos, versão, serviços, intents duplicadas, estado)
  rejeitado antes de efeitos de recovery. Não é alteração dos schemas PR1.
- Pais symlink/junction e destinos inseguros de journal rejeitados; reserva
  hardlinked não pode truncar outro arquivo. Artefatos com identidade/timestamps
  alterados durante hashing são rejeitados e continuam parciais.
- Archives conferem também nomes/identidade dos diretórios, pois timestamps
  Windows podem ser adiados; adição/remoção concorrente não é aceita silenciosamente.
- Ferramentas PostgreSQL usam socket Unix e usuário postgres explícitos, sem
  password/host externo em argv. O gate cobre client backends de todo o cluster,
  inclusive outros bancos que poderiam alterar roles/globals.
- Falha de catálogo tem código DUMP_CATALOG_INVALID; timeout/cancelamento mantêm
  seus códigos. Catálogo válido não comprova integridade de todos os payloads
  nem restauração funcional.
- A proteção contra segundo SIGTERM cobre journal de falha e cleanup inteiro,
  não somente a chamada de retorno de serviços.

Testes negativos conferem códigos específicos, não apenas qualquer exceção.
Matriz sintética percorre dez fases interrompidas; não executa SIGKILL em Docker.
Filho Python encerra com os._exit sem finally e o lock fica novamente disponível
no Windows. Isso não prova flock/reboot/fsync do Linux.

| Interrupção | Resultado confirmado / pendência |
| --- | --- |
| Falha de cada etapa, timeout/cancelamento injetados | Mock confirma candidato incompleto, preservação de parcial e retorno controlado |
| Segundo cancelamento durante cleanup | Handler protegido confirmado localmente; sinal real Linux pendente |
| Intent de manutenção sem fingerprint confirmado | Recovery rejeita ownership ambíguo, sem desligar manutenção |
| Manutenção desligada, crash antes de gravar novo fingerprint | Recovery rejeita por MAINTENANCE_OWNERSHIP_CHANGED; intervenção necessária |
| Journal pending ou filesystem sem gravação | Sem adoção/overwrite automático; retorno pode exigir intervenção |
| SIGKILL/controlador morto/reboot | Journals sintéticos testados; supervisor, processos Docker órfãos e reboot reais pendentes |

Fingerprint não é um lock distribuído nem prova criptográfica de ownership.
Todas as operações de manutenção precisam cooperar com a exclusão local. Um
operador que regrave exatamente a mesma configuração pode não ser distinguível.
Docker restart/exec, host cron e escritores privilegiados exigem fencing e
controle externos antes de integração operacional. Não houve tentativa de
resolver esses riscos com permissões maiores ou com recuperação destrutiva.

Conteúdo sensível: config inteira preserva instanceid/passwordsalt/secret e
configurações mutáveis; HTML inclui apps/temas/dados locais e cópia do config para
evitar exclusões silenciosas. PGDATA/cache/observabilidade/state são excluídos.
Secrets PostgreSQL/Redis e arquivos esperados pelo Compose/instalador são
preservados somente em parâmetros privados para futura reconstrução da mesma
composição; não há leitura de perfis AWS/GitHub ou do secret dedicado Grafana.
Como config/, SQL global e .env são preservados inteiros, podem conter outros
valores sensíveis configurados pelo operador: permanecem privados, não são
classificados como conteúdo publicável. O mapeamento desses parâmetros
para uma restauração real ainda exige ensaio e revisão, não está implementado.

## Proposta de ensaio Linux descartável — exige autorização própria

Não instalar ferramentas no computador e não usar a EC2 existente. Preparar,
após aprovação, uma VM Ubuntu 24.04 descartável e sem credenciais AWS/GitHub,
sem mounts de dados operacionais, com Docker/Python/dependências/imagens fixadas
já disponíveis ou instalação separadamente autorizada. Código somente de leitura;
staging e volumes sintéticos dedicados. Nenhuma AWS endpoint deve ser acessível.

1. Executar as suítes POSIX do PR2, inclusive truncamento e FIFO/socket, antes
   de qualquer integração operacional. Ainda **não permitido usar o validador
   operacionalmente em Linux**.
2. Executar os testes PR3 de subprocessos reais (timeout, streaming e filtragem
   de ambiente), locks, fsync/rename, paths e disco/quota sintéticos.
3. PostgreSQL 17.11 exato: dados sintéticos, app role/grants/extensões/ownership;
   comparar dump/catalog e metadados. Testar indisponibilidade e processos lentos.
4. Definir/revisar harness Nextcloud 34.0.4 e provider S3/IMDS **locais emulados**,
   sem credenciais reais. O adaptador mínimo atual não admite containers extras:
   a composição/fencing desse harness precisa de revisão, não de relaxamento
   automático das guardas. Emulação de testes não é implementação do PR5.
5. Exercitar requisições em andamento, jobs/cron/executores externos, Docker
   restart/StopSignal, installed=true e hooks; demonstrar drenagem ou rejeição.
6. SIGTERM, SIGKILL do controlador, reboot da VM e falha de Docker/journal/disco;
   comprovar recovery sem desativar manutenção externa ou repetir dump.
7. Verificar permissões e ausência de secrets em argv/logs/saída. Nenhum resultado
   permite backup real ou restore de produção sem nova autorização.

Separação objetiva do ensaio:

- **Automatizável sem containers:** PR2 completo com zero skips POSIX (truncamento,
  FIFO/socket), PR3 subprocessos/ambiente/timeout, symlink/hardlink/traversal,
  flock após kill, fsync/rename com injeção de erro, journal e estados sintéticos.
  Syntax-check Ansible só quando ferramentas já estiverem presentes na VM.
- **Exige containers reais:** autenticação Unix PostgreSQL, imagens exatas,
  dump custom e catálogo reais, SQL globals/roles/ACL/extensões, hooks e fingerprint
  Nextcloud, requisições em andamento, cron/jobs e parada graciosa Apache.
- **Exige VM/supervisor real:** SIGKILL com exec Docker remanescente, falha do
  daemon, ENOSPC/quota e reboot; verificar preservação de manutenção e journal
  no retorno. Matar o cliente Docker não prova encerramento do pg_dump no container.

VM sem egress operacional, sem metadata/AWS endpoints, perfis/keys/tokens ou
SSH para produção. Dependências/imagens devem ser pré-carregadas com autorização
separada. Artefatos e métricas do ensaio permanecem locais e sintéticos. Critério
de avanço: zero skips POSIX pertinentes e evidências da matriz real; nenhum desses
resultados concede autorização de Destroy ou comprovação funcional de restore.

## Ansible e rollback

Role/playbook novos apenas preparam fontes públicas e staging em Linux local
isolado; não estão importados por bootstrap/deploy. Não instalar/ativar a unidade
systemd nem criar containers na preparação. O rollback deste PR local é retirar
somente essas fontes novas/docs; não há recurso ou dado operacional para desfazer.
Capturas futuras com falha devem manter arquivos e journal para diagnóstico.

Execução, formatos e códigos: [README da captura](../scripts/backup/capture/README.md).
