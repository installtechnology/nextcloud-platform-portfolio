# PR7 — observador Alloy na rede PostgreSQL

Correção aprovada para publicação; não aplicada à EC2.

O Compose existente conecta Alloy a backend e observability. Sua configuração
fixada usa Blackbox para GET de status do Nextcloud e sondagens TCP de PostgreSQL
e Redis. A sondagem TCP testa conexão, sem autenticação/SQL. A configuração não
contém credenciais de banco. Alloy também lê logs Docker por bind somente leitura.

O guard anterior admitia somente Nextcloud, PostgreSQL e Redis na rede do banco,
portanto rejeitava o observador legítimo. A correção reconhece exclusivamente um
Alloy da receita aprovada, sem lista configurável ou flag de aprovação:

- Labels Compose do projeto, serviço e ausência de one-off.
- Imagem fixada, image ID e RepoDigest, entrypoint e argumentos exatos.
- Rootfs somente leitura, sem privilégios, capabilities adicionais, portas
  publicadas ou namespaces host; no-new-privileges obrigatório.
- Somente as duas redes previstas, mounts exatos e volume exclusivo de Alloy.
  Nenhum socket Docker, volume Nextcloud/PostgreSQL ou secret de aplicação.
- Variáveis limitadas à receita, sem credenciais AWS/banco.
- Arquivos de configuração root, sem escrita por grupo/outros ou symlinks,
  com leitura estável e hashes da receita revisada. No Blackbox, só o valor
  escalar seguro do header Host é normalizado; módulos GET/TCP ficam fixados.

As verificações ocorrem no baseline, antes de manutenção/parada, e se repetem na
janela. Peer desconhecido, duplicado, identidade divergente ou receita alterada
bloqueiam a operação. Mudanças legítimas da receita exigem revisão dos pins.

Reconhecer o observador não demonstra quiescência. LiveCaptureBackend continua
exigindo HBA verificado que rejeita TODOS os clientes TCP, drenagem/ausência de
sessões, Nextcloud parado, auditoria cron/CLI/processos/mounts e fencing S3.
As sondagens TCP não recebem exceção de autenticação no HBA. Manutenção original,
journals, supervisor, recovery e interlocks não foram alterados.

Testes locais usam mocks e arquivos sintéticos. Não comprovam o comportamento do
container real nem a compatibilidade de seus metadados Docker. A instalação futura
deve executar o baseline completo antes de permitir uma janela de captura.

Os pacotes oficiais do plugin 1.2.707.0 e age 1.3.2 foram adquiridos em temporário
fora do Git, sem instalação ou execução. O resultado com digests fica em registro
local privado. Plugin: assinatura GPG verificada e fingerprint conferido com a
[documentação AWS](https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-linux-verify-signature.html).
age: SHA-256 publicado e assinatura Ed25519 do autor na prova verificados com as
[chaves oficiais da versão](https://github.com/FiloSottile/age/blob/v1.3.2/SIGSUM.md).
Isso não equivale à verificação Sigsum completa: inclusão/log/testemunhas e quorum
não foram validados pelo verificador oficial, indisponível neste computador.
As versões foram conferidas estaticamente; a execução futura de --version continua
dependendo de autorização própria.

Validação local final: 26 testes novos aprovados; suíte PR7 com 335 aprovados e
3 skips de kernel Linux neste Windows (esses testes já foram homologados antes
desta mudança). PR1: 73; PR2: 187 e 2 skips; PR3: 71 e 3 skips; PR6: 146;
qualificação: 8; controlador: 15. Verificações de sintaxe, secrets e diff-check
aprovadas. Nenhum pacote adquirido foi executado ou instalado, nem houve acesso
à infraestrutura operacional. Os testes de assinatura age rejeitam truncamento,
assinatura alterada e chave incorreta. Não foi feita verificação Sigsum completa.
