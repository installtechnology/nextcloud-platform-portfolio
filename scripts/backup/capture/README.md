# PR3 — captura local, somente ensaio isolado

Implementação preparada para testes e integração futura. **Uso operacional
bloqueado**: faltam os testes POSIX do PR2 e ensaios Linux/Docker/PostgreSQL/
Nextcloud reais. O comando atual não aceita a stack operacional: exige projeto
`nc-capture-test-*`, root Linux, daemon Unix local e fixture isolada.
Não existe modo operacional, SSH, chamada AWS/S3, workflow, upload ou restore.

## Fluxo implementado

1. Lock local exclusivo e conferência de journal anterior não resolvido.
2. Preflight de imagens fixadas no Compose, PostgreSQL major 17, cliente de dump,
   instalação/identidade S3, serviços/mounts, isolamento e espaço local estimado.
3. ID UTC + UUID v4, diretório privado, journal com intenções persistidas por
   fsync/rename e reserva de espaço para registrar uma falha de disco.
4. Conferir manutenção/fingerprint original, habilitar manutenção e registrar
   ownership pelo fingerprint. Não adotar uma transição de outro operador.
5. Parar Nextcloud pelo StopSignal WINCH da imagem Apache, esperar saída zero,
   recusar SIGKILL/erro/OOM e confirmar ausência de escritores/sessões pares no DB.
6. `pg_dump --format=custom --create`, preservar ownership/ACL e conferir catálogo
   com `pg_restore --list` (isso **não** comprova restore).
7. Capturar `config/`, todo HTML e parâmetros protegidos; conferir novamente
   quiescência. Não copiar PGDATA em execução.
8. Restaurar somente estados controlados, confirmar manutenção/serviços anteriores
   e classificar o resultado como **local_candidate_only**, nunca backup completo.

Manutenção preexistente não é regravada. Antes de retirar manutenção iniciada
pela captura, conferir serviços e PostgreSQL/Redis; indisponibilidade mantém
manutenção e exige intervenção. O gate de sessões cobre todo o cluster, incluindo
outros bancos que poderiam alterar globals; não constitui fencing contra root.

Maintenance mode sozinho não drena requisições nem impede cron/CLI/admins. A parada
graciosa encerra os processos do container antes da captura. A fixture aceita
somente Nextcloud/PostgreSQL/Redis, redes internas sem publicação, volumes locais
rotulados e exclusivos e binds dentro do diretório sintético autorizado. Qualquer
container externo ligado a esses volumes/redes bloqueia a captura. Não há cron
separado no Compose atual. Um cron/container/job externo, host cron, deploy,
administrador ou escritor S3 exige fencing adicional antes de qualquer integração
operacional: a inspeção Docker não prova exclusão desses atores privilegiados.

## Conteúdo e PostgreSQL

| Arquivo candidato | Conteúdo |
| --- | --- |
| `database.dump` | Banco da aplicação, custom; cliente major 17; ownership/ACL e criação do DB |
| `configuration.tar` | Todo config/, arquivos adicionais, identidade e `.platform-storage.json` |
| `local_files.tar` | Todo nextcloud_html, inclusive dados locais, apps/temas/core/config, sem exclusões presumidas |
| `recovery_parameters.json` | Metadados DB/owner/encoding/collation/extensões, globals sem passwords, secrets e env protegidos, código runtime e imagens |
| `journal.json` | ID/janela, estados, intenções, hashes plaintext e diagnóstico local |

Roles/grants globais são capturados explicitamente por `pg_dumpall --globals-only
--no-role-passwords`; não estão implícitos no dump da aplicação. Tablespaces
customizados são rejeitados. Para recuperação futura: revisar SQL global em cluster
isolado, reconstruir owner/memberships/grants e instalar extensões compatíveis;
reaplicar credenciais a partir dos secrets protegidos, sem argv. Não existe código
de restauração aqui. Outros bancos do cluster não são capturados. Redis e volumes
de observabilidade permanecem excluídos; observabilidade vem do código publicado.
psql/pg_dump/pg_dumpall usam explicitamente o socket /var/run/postgresql e a role
postgres. pg_restore --list confere o catálogo, sem ler/restaurar necessariamente
todos os payloads; sua aprovação não comprova dump restaurável.

Base64 nos parâmetros é **codificação, não criptografia**. Dumps, archives,
parâmetros e journal permanecem locais, sensíveis, em staging 0700/files 0600.
Não enviar ao GitHub, logs, artifacts ou remoto. Não guardar no checkout. A futura
criptografia/transferência/inventário é PR5 e não foi implementada.

Arquivos são escritos como `.partial`, com limite de bytes durante o streaming;
somente depois de fsync/tamanho/hash recebem o nome final. Nenhum parcial é
apagado ou reutilizado automaticamente. Links e arquivos especiais no volume
exigem classificação e bloqueiam captura; archives não seguem links externos.
UID/GID/mode são preservados pelo tar. Estimativa de espaço não garante capacidade:
ENOSPC ainda precisa de tratamento e teste em filesystem isolado.
Mudanças de identidade/timestamps de artefatos durante hashing e mudanças nos
nomes dos diretórios durante archive bloqueiam finalização. Pais de journal com
links/reparse points e reserva hardlinked são rejeitados.

## Interrupção e recovery controlado

`recover` significa **retorno do estado de serviço**, não restore de dados. Usa
o ID exato de `active.json`, nunca latest, não repete dump e abandona o conjunto.
Lock OS impede concorrência. SIGINT/SIGTERM acionam tratamento; cleanup ignora
cancelamento repetido durante operações com timeout. SIGKILL, reboot ou perda do
controlador não executam finally: journal e supervisão externa são necessários.

O template systemd possui `ExecStopPost` de recuperação e timeout; é entregue como
arquivo inativo, sem daemon-reload/start/enable. Sua eficácia ainda exige ensaio
real Linux com kill/reboot, bloqueios e falha do Docker daemon. Sem supervisor,
o operador precisará executar recovery controlado depois da interrupção.

Se ownership/fingerprint/identidade mudaram, o controlador **não desliga manutenção**
nem tenta corrigir a stack. Uma interrupção entre maintenance intent e registro do
fingerprint pode exigir intervenção. Journal `.pending`, filesystem corrompido,
indisponível ou sem espaço para registrar recuperação também exigem revisão; não
apagar pending/artefatos, não alterar journal manualmente nem liberar operação nova
sem procedimento específico. Todos os operadores de manutenção precisam respeitar
a mesma exclusão local; não há proteção contra root malicioso ou writer não isolado.
Fingerprint não distingue necessariamente a regravação idêntica por outro
operador. Crash depois de maintenance off e antes do novo fingerprint durável
exige intervenção; não inferimos ownership para reexecutar maintenance off.

Um timeout mata o cliente Docker local. Um exec remoto já iniciado pode sobreviver
temporariamente; dump é read-only e o candidato falhou. Antes de nova tentativa,
é obrigatório verificar que não há sessão/processo remanescente. Não usamos
pg_terminate_backend, limpeza de DB ou destruição automática.

## Execução futura em fixture autorizada

Somente depois de aprovar um Linux descartável já preparado, executar primeiro
as suítes POSIX do PR2. Não instalar ferramentas automaticamente.

```text
python -m scripts.backup.capture start --isolated-project nc-capture-test-fixture --fixture-root DIRETORIO_FIXTURE --staging DIRETORIO_PRIVADO
python -m scripts.backup.capture recover --isolated-project nc-capture-test-fixture --fixture-root DIRETORIO_FIXTURE --staging DIRETORIO_PRIVADO
python -m unittest discover -s scripts/backup/capture/tests -p "test_*.py" -v
```

CLI retorna JSON sanitizado: `LOCAL_CANDIDATE_ONLY`, `RECOVERY_REQUIRED`,
`CAPTURE_BUSY`, `MAINTENANCE_OWNERSHIP_UNCERTAIN`, `COMMAND_TIMEOUT`, etc.
Exit 0: operação local concluída/terminal; 2: falha conhecida; 3: erro interno
sanitizado. Sucesso não significa backup completo/íntegro/restaurável nem autoriza
Destroy. Não são produzidos manifesto v1, object_inventory ou completion marker.
SHA-256 aqui é de plaintext; nunca preencher com ele o hash ciphertext do PR1.

Os testes PHP opcionais usam `CAPTURE_TEST_PHP` e `CAPTURE_TEST_OC_CONFIG`
apontando para ferramentas/fontes oficiais já disponíveis. Sem elas os testes
declaram skips, sem download. No ensaio final, usar a fonte da imagem exata.

## Ansible

`prepare-capture-sandbox.yml` é separado de deploy e limitado a conexão local,
Linux/root e projeto de teste. Prepara arquivos públicos e staging privados,
sem instalar dependências, criar containers, ativar unidade ou executar captura.
Não foi executado nesta entrega. Nenhuma role/playbook existente o importa.

Plano de ensaios e resultados: [docs/phase-7-pr3.md](../../../docs/phase-7-pr3.md).
