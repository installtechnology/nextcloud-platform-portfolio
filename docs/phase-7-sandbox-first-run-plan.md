# Primeira execução isolada — plano para revisão

Preparação local de 2026-10-10. Base publicada: f885202680b2ee01dcde1decab5df47812a7bdb3.
Nenhuma execução está autorizada por este documento. Não há implementação de
launcher neste patch. A [fixture publicada](../scripts/backup/sandbox/README.md)
e seus 59 testes aprovados não serão auditados novamente sem mudança concreta.
O [desenho S3/IMDS](phase-7-s3-imds-fixture-design.md) permanece a referência.

## Windows observado, somente leitura

| Item | Resultado |
| --- | --- |
| Sistema | Windows 11 Pro x64, versão 10.0.26100 |
| CPU | AMD FX-8320E; WMI reporta 4 cores e 8 processadores lógicos |
| RAM física | 16,00 GiB |
| RAM livre na leitura | 2,12 GiB; valor varia durante o uso |
| Disco C: livre | 0,91 GiB |
| Disco D: livre | 6,07 GiB |
| Disco E: livre | 236,09 GiB |
| Virtualização firmware / VM monitor / SLAT | true / true / true |
| HypervisorPresent | false |
| Hyper-V / Windows Hypervisor Platform | desabilitados |
| Virtual Machine Platform / WSL | habilitados |
| WSL | padrão versão 2; consulta de distribuições não retornou nomes |

Serviço vmcompute parado; WslService em execução. Não foram encontrados executáveis
VirtualBox/VMware nos caminhos usuais consultados; isso não prova ausência em todos
os locais. WSL habilitado não comprova VM Ubuntu utilizável. Nenhuma distribuição
foi iniciada. Consulta CIM precisou execução fora do sandbox da ferramenta, sem
modificar recursos do Windows. A disponibilidade reportada não substitui um boot
real do guest nem valida compatibilidade de todos os drivers deste hardware.

## Alternativa recomendada e recursos

Preferir Hyper-V já disponível na edição Pro, após autorização para habilitar e
reiniciar Windows. Usar Ubuntu Server 24.04 AMD64, VM dedicada, 4 vCPU, memória
fixa 8 GiB e VHDX de 40 GiB em E:. Não armazenar a VM em C: ou D:.
Reservar aproximadamente 60 GiB em E: para VHDX, ISO, quatro imagens e evidências;
medir tamanhos reais antes do download. 40 GiB é proposta para dataset pequeno,
não garantia de capacidade para futuros dumps/ENOSPC.

Não iniciar VM de 8 GiB enquanto a memória livre do host não atingir pelo menos
10 GiB. Fechar aplicações voluntariamente; não matar processos automaticamente.
Liberar espaço em C: sob decisão do operador antes de instalação/reboot; objetivo
prático inicial de 10 GiB livres, não requisito oficial universal. Memória fixa
evita que o laboratório dependa de sobrealocação silenciosa. Uso intenso causará
contenção de CPU/RAM e possível paginação neste computador antigo.

Se não for possível liberar recursos: primeira opção sem cobrança é outro PC
emprestado com Ubuntu24.04, >=8 GiB disponíveis e disco adequado. Uma VM 2 vCPU/
6 GiB pode ser avaliada somente para instalação/roundtrip mínimo, com >=8 GiB
livres no host, sem presumir homologação dos ensaios posteriores. Não reduzir
memória para fazer passar OOM. WSL2 não é a escolha deste ensaio: compartilhamento
com o host e testes futuros de reboot/systemd exigem controles adicionais.
Runner hospedado exigiria nova revisão de execução, isolamento e custo/minutos;
não reutilizar o workflow POSIX para containers.

Hyper-V/Ubuntu/Docker Engine/Moto não exigem novo serviço AWS ou Docker Desktop.
Há consumo de disco, energia, internet e tempo. Não foi contratado serviço pago.
[Requisitos Hyper-V](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/host-hardware-requirements),
[edições e habilitação](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v?pivots=windows&tabs=gui).

## Duas etapas de rede, sem aplicação durante downloads

1. Preparação após autorização: criar guest limpo; instalar Docker Engine/Compose
   e dependências mínimas aprovadas no guest. Baixar ISO oficial Ubuntu com
   SHA256SUMS/assinatura autenticados; registrar versão da ISO e procedência.
   Conferir versões Docker/Compose/OpenSSL/Python/curl antes de escolher comandos.
   Usar conexão temporária somente para fontes oficiais; não levar credenciais
   AWS, chaves SSH, profiles, GitHub token, VPN ou pastas do operador para o guest.
2. Pré-carregar exatamente quatro referências de imagens do spec.py publicado,
   com plataforma linux/amd64. Não usar tag isolada, latest ou fallback. Moto
   serve dois serviços, mas é uma imagem. Conferir manifesto, digest solicitado,
   RepoDigests, OS/Architecture, IDs e versões internas após download autorizado.
   Registrar as dependências efetivas; metadados anteriores não provaram camadas.
3. Copiar somente fontes públicas selecionadas do commit fixado via mídia ISO
   somente leitura, com lista de SHA-256. Sem compartilhamento de diretório,
   clipboard, drive redirect, Docker socket Windows ou remote Docker context.
   Desanexar mídia após importação. Segredos sintéticos são gerados no guest.
4. Antes de iniciar qualquer serviço, desconectar TODOS os adaptadores da VM de
   switches/NAT/bridge. Operar pela console Hyper-V; nenhuma porta da VM publicada.
   Não usar Default Switch durante ensaios. Snapshot de baseline antes dos dados.
5. Guest e Docker: manter redes db_internal/storage_internal internas; impedir
   saída externa e acesso do bridge ao host/link-local, IPv4 e IPv6. Host INPUT,
   OUTPUT e FORWARD têm allowlist mínima: loopback, DNS interno necessário,
   tráfego entre os cinco serviços conforme o modelo e cliente de teste local
   para endpoints exatos. Não encaminhar DNS para resolvers externos.
   Implementar regras compatíveis com o backend firewall efetivo do Docker;
   não desativar o firewall Docker nem confiar só em UFW/internal=true.

A desconexão no hypervisor é a barreira independente de egress. Regras do guest
complementam isso e preservam comunicação interna. Não basta remover uma rota
default: verificar interfaces, IPv6, proxies, forwarding e rotas especiais.
[Firewall Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

Antes da aplicação, testes de isolamento usarão somente destinos artificiais
reservados (192.0.2.1/2001:db8::1), inspeção de regras e rejeição no caminho de
firewall. Nenhuma requisição a AWS real ou 169.254.169.254/fd00:ec2::254.
Para link-local real, provar rota/regra DROP por inspeção e teste unitário da regra;
não enviar pacote ao IMDS. Falha de ping isolada não é prova de isolamento.
No mesmo VM/bridge, avaliar a regra contra destino sintético antes da aplicação,
sem sexto serviço/container. Revalidar depois de Docker criar suas chains.

## Launcher mínimo proposto — não implementado

Arquivo futuro separado: scripts/backup/sandbox/lab_controller.py e testes próprios.
Não modificar PR3, CLI sandbox atual, schemas ou deploy. Sem imports de capture,
sealing ou orquestração operacional. Comandos previstos: prepare, preflight,
start, verify, stop. Nenhum comando capture/backup/restore/destroy ou completion.

Autorização proposta: confirmação literal RUN_SYNTHETIC_LAB mais ID do projeto e
baseline hash fixado no código revisado; verificação presencial de VM dedicada,
Linux24.04/x64, daemon local e ausência de outros projetos. Não aceitar configuração
que altere allowlist, pins, endpoints, contexto Docker, serviços ou modos de captura.
Esses controles técnicos não substituem autorização humana e não resistem a um
administrador que modifique o código/daemon. Aprovar este desenho não implementa
nem autoriza o launcher. Não aceitar flags booleanas como evidência de isolamento.

O launcher coletará estado real do hypervisor/guest e Docker, validará root privado
fora do checkout, mounts regulares RO e sem symlink, permissões/UID, certificados,
labels/namespaces, imagens e rede. Evidência de desconexão será fornecida por coleta
read-only no host Windows e conferida com a topologia guest. Sem evidência válida,
não iniciar. runtime_allowed=false e backup_complete=false do código atual nunca
mudam: validação sintética não concede autorização operacional.

Estado local do laboratório acompanha etapas e IDs dos containers/redes/volumes.
Após erro: coletar códigos sanitizados, parar; não reinstalar, reexecutar sem
controle ou remover volumes. stop verifica ownership/IDs antes de parar somente
os cinco containers. Preservar volumes/artefatos; não executar down -v, prune ou
remoção genérica. Não criar supervisor de captura neste ensaio mínimo.

Testes negativos necessários antes de aprovar o launcher:

- confirmação/commit/projeto/VM incorretos; Windows ou distro/plataforma diferente;
- adaptador externo conectado, rota/proxy/DNS externo ou regra egress insuficiente;
- Docker remoto/socket alternativo, sexto serviço ou consumidor externo de mounts;
- pin/image ID divergente, portas públicas, namespace compartilhado ou bind externo;
- CA/hostname/validade incorretos, key TLS acessível indevidamente, SDK inesperado;
- variáveis AWS operacionais, profiles/credenciais reais, material sintético ausente;
- falha a cada etapa: nenhum install, captura ou completion indevido; stop só próprio.

## Bundle e bootstrap dos cinco serviços

Root futuro /srv/nc-s3-imds-test-<16hex>, privado0700; code/tls/secrets/evidence
com permissões restritas. Não escolher root dentro do checkout operacional.
Código vem do commit fixado: configure-nextcloud.sh, storage-guard.php,
install-nextcloud.php original, nextcloud-healthcheck.php, postgres-init.sh,
redis-entrypoint.sh e arquivos de fixture montados nos destinos já publicados.
Conferir bytes, modos executáveis dos hooks shell, proprietário e acesso real de
Compose file secrets pelo consumidor. Não supor que uid/gid/mode Compose corrijam
permissões de um bind no host. Nunca imprimir inspect integral/env/config.php.

Gerar CA e certificados exclusivamente sintéticos: SAN s3.sandbox.test e
fixture-data.s3.sandbox.test para Moto; imds.sandbox.test para IMDS. Chaves TLS0600
legíveis UID1000; CA pública nos consumidores; relógio correto no guest. Senhas
fortes distintas em quatro arquivos0600, sem argv, logs ou artifacts GitHub.
Validar CA/chain/SAN/hostname com casos positivo e negativo, sem verify=false.

Ordem prevista:

1. Preflight de host/root/daemon/firewall e hash do bundle. Criar exatamente os
   cinco containers parados e revalidar mounts/redes reais antes de iniciar
   qualquer um; Nextcloud permanece parado até o bootstrap S3 terminar.
2. Iniciar Moto e IMDS, PostgreSQL e Redis. Verificar TLS e IMDSv2 token/role com
   cliente guest apontado exclusivamente aos IPs privados e hostnames aprovados.
   Credenciais fictícias ficam em memória; saída somente status/role/expiração.
3. Cliente local de bootstrap S3 precisa implementação mínima ainda revisada:
   HTTPS com CA/hostname, endpoints/IPs coletados do próprio projeto, obtenção de
   credenciais SOMENTE do stub e SigV4. Criar fixture-data e habilitar versionamento,
   verificar vazio/versões/delete markers sintéticos. Não instalar AWS CLI, não
   usar cadeia default que procure credenciais reais, não criar sexto serviço.
   Rotas/allowlist guest permitem apenas esses endpoints; cleanup de objetos não
   faz parte da primeira execução. Configuração localhost por IP sem SAN não basta.
4. Criar/verificar/iniciar Nextcloud somente após gates anteriores. Preflight PHP
   confere SDK3.376.2. Hooks originais/guard/instalador com wrapper exclusivamente
   bucket_empty completam instalação sintética. Banco inesperadamente não vazio
   ou config incompatível provoca parada, sem limpar tabelas/volumes.
5. Cliente guest usa Nextcloud no IP privado:80, sem publicar portas. URL é interna
   ao guest; navegador Windows não é necessário. WebDAV cria arquivo sintético
   pequeno, baixa e compara SHA-256. Senha lida de arquivo/FD privado, nunca URL,
   argv ou logs. Conferir objeto por VersionId via cliente S3 local, sem assumir
   que upload/download isolado comprova inspeção S3.
6. Coletar evidências sanitizadas e parar serviços preservando volumes. Moto RAM
   perde objetos/VersionIds ao encerrar; esse laboratório não é backup persistente.

## Comandos futuros para revisão, não executados

No Windows, após Hyper-V autorizado e VM criada com nome fixo:

```powershell
Get-VM -Name 'nc-s3-imds-lab' | Select-Object Name,State,MemoryAssigned
Get-VMProcessor -VMName 'nc-s3-imds-lab' | Select-Object Count
Get-VMNetworkAdapter -VMName 'nc-s3-imds-lab' | Select-Object Name,SwitchName
# Somente depois dos downloads autorizados e antes de subir serviços:
Disconnect-VMNetworkAdapter -VMName 'nc-s3-imds-lab'
Start-VM -Name 'nc-s3-imds-lab'
vmconnect.exe localhost 'nc-s3-imds-lab'
# Após shutdown limpo no guest:
Get-VM -Name 'nc-s3-imds-lab' | Select-Object Name,State
```

O Start-VM acima representa o boot isolado, não a etapa conectada de provisionamento.
No guest, interface PROPOSTA, indisponível até implementar/aprovar o launcher:

```text
python3 /opt/nc-sandbox/lab_controller.py prepare --project <ID> --source-commit f885202680b2ee01dcde1decab5df47812a7bdb3
python3 /opt/nc-sandbox/lab_controller.py preflight --project <ID>
python3 /opt/nc-sandbox/lab_controller.py start --project <ID> --confirmation RUN_SYNTHETIC_LAB
python3 /opt/nc-sandbox/lab_controller.py verify --project <ID>
python3 /opt/nc-sandbox/lab_controller.py stop --project <ID>
sudo shutdown -h now
```

Não rodar Compose bruto. O controller futuro usa Compose por IDs/projeto/endpoints
validados, pull=never e exatamente os cinco serviços. Falhas preservam o conjunto
para diagnóstico; nenhuma rotina de exclusão de dados acompanha stop.

## Aceite objetivo e evidências

- Ubuntu24.04 AMD64/Docker/Compose/SDK/versões registrados; quatro pins sem fallback.
- Todos adaptadores externos desconectados; ausência de rotas/proxies/DNS externo;
  evidências das regras e testes sintéticos IPv4/IPv6, antes/depois do start.
- Exatamente cinco containers com ownership correto e nenhuma porta publicada.
- TLS CA/SAN válido; CA/hostname inválidos rejeitados; IMDSv1 rejeitado, token
  IMDSv2/role fictícia aceitos, nenhuma resposta de credencial registrada.
- S3 fixture-data versionado; testes VersionId/delete markers/paginação coerentes;
  PR5 mantém AMBIGUOUS_CURRENT_VERSION para respostas inválidas.
- PostgreSQL/Redis/Nextcloud healthy; Moto/IMDS aprovados por probes específicos,
  pois seus serviços publicados não têm healthcheck Docker próprio.
- occ status installed=true; objectstore S3/bucket/região/endpoint sanitizados;
  login/WebDAV e bytes sintéticos upload/download iguais, objeto inspecionado.
- Rechecagem pós-instalação não reinstala nem altera identidade; sem nova captura.
- Evidências: IDs/pins/versões/status/hashes/duração/reason codes. Proibido exportar
  DB/config.php/env/credenciais/tokens ou logs completos sem sanitização.
- Nenhum backup_complete, restore comprovado ou autorização para Destroy derivado.

## Arquivos necessários e pendências, sem ampliar escopo

Neste patch: somente este plano. Futuro patch estritamente de sandbox: controller,
cliente S3/IMDS/WebDAV local com TLS e credenciais em memória, coletor sanitizado,
geração de bundle/CA/secrets e testes negativos. Reutilizar módulos existentes;
não acrescentar adapter operacional/captura/systemd/restore nesta inicialização.
Compose permanece intacto agora; se cliente/bundle exigir mount adicional ou
descobrir incompatibilidade real, apresentar diff mínimo para aprovação primeiro.

Autorizações seguintes: habilitação/reboot Windows, criação VM e downloads/instalação
no guest; implementação/revisão do launcher e depois uma execução isolada.
Não dar autorização implícita por editar variável. Nenhuma senha/chave definitiva
de recuperação será gerada. Hardware/memória/C: precisam preparação antes do boot.

Estimativa de trabalho, não prazo garantido: patch mínimo/negativos 2–4h; habilitar/
reiniciar/preparar Ubuntu+Docker+imagens 1–3h com internet adequada; primeiro ensaio
e evidências 30–90min após gates. Total aproximado4–9h, em etapas autorizadas;
compatibilidade runtime ainda não comprovada pode interromper antes do install.
Destrutivos SIGKILL/reboot/ENOSPC, captura/fencing e restore ficam para etapas
posteriores. Não repetir auditorias históricas nem afirmar que estão homologados.
