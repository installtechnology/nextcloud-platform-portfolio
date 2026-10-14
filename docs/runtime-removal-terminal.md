# Remover os ambientes pelo terminal

## Execucao de 14/10/2026

Remocao administrativa pelo terminal concluida com dois planos novos: 16
recursos descartaveis do ambiente original e 17 do ambiente recriado. Os dois
applies terminaram sem criacoes ou atualizacoes. A API AWS confirmou as duas
EC2 em `terminated`, os dois volumes root ausentes e as duas VPCs ausentes.
O state runtime ficou sem recursos gerenciados; o restore manteve os sete
persistentes. Os quatro buckets (dados de origem, dados do destino, backups e
state) continuaram acessiveis e com versionamento ativo.

Antes da remocao foram reconferidas as versoes imutaveis dos cinco artefatos
de um backup completo previamente validado e as 173 versoes de dados de origem.
Completion, manifest e inventario foram conferidos; esta consulta nao repetiu
o download e SHA-256 integral de todos os payloads. Os workflows operacionais
estavam desativados e nao havia execucao em andamento. Esta entrega nao cria
um workflow Destroy e nao altera os indicadores formais de restore/destroy.

O deploy nao executa destroy. A remocao usa um plano salvo do Terraform.
Somente os recursos descartaveis sao removidos: EC2 (incluindo seu disco root),
rede e IAM da aplicacao. Dados locais no disco root sao perdidos. Os buckets
de dados, backups e state, o bootstrap e o root persistent permanecem.

## Gerar uma previa

Execute na raiz do repositorio, com Python, Terraform e credenciais AWS
configurados. Use arquivos privados JSON de variaveis e o backend correto do
ambiente. Nao publique esses arquivos ou o diretorio `.validation-tools`.

```powershell
python scripts/plan-runtime-removal.py --root runtime --backend "CAMINHO/runtime.tfbackend" --variables "CAMINHO/runtime.tfvars.json" --instance "ID_EC2_ORIGINAL"
python scripts/plan-runtime-removal.py --root restore --backend "CAMINHO/destination.tfbackend" --variables "CAMINHO/destination.tfvars.json" --instance "ID_EC2_RECRIADA"
```

O comando confirma a EC2 contra o state, gera um diretorio privado novo,
mantem locking no backend S3 e imprime os enderecos que serao apagados.
No root restore, exclui explicitamente os sete recursos persistentes do alvo.
Qualquer tentativa de incluir recursos S3 na remocao e rejeitada.
Nao reutilize um plano antigo depois de recriar o ambiente.

## Conferir e aplicar

Antes de aplicar, confirme que existe um backup completo e valido, que suas
versoes de dados continuam disponiveis e que nao ha deploy, backup ou restore
em andamento. O gerador de planos nao faz essa validacao do backup por voce.
Confira os recursos em `review.json`; nenhuma criacao ou atualizacao deve
estar planejada. Os sete persistentes do restore devem estar preservados.
Os dados de origem no S3 sao necessarios para o modelo de recuperacao atual.

O comando imprime a linha exata para aplicar o plano salvo. Ela tem este formato:

```powershell
terraform "-chdir=CAMINHO_DO_DIRETORIO_GERADO" apply -input=false "CAMINHO_DO_PLANO/removal.tfplan"
```

Esse e o passo que remove os recursos. Execute somente para os planos que
acabou de conferir. Nao execute `terraform destroy` geral no root restore.
Nao use force-unlock, state rm ou import para contornar falhas. Se o apply
falhar, preserve os logs privados e confira o estado antes de tentar novamente.

Depois, consulte as duas EC2 pela API e confira `terraform state list` nos dois
diretorios usados. O runtime original deve ficar sem recursos gerenciados;
o restore deve conservar somente os sete recursos persistentes. Confirme tambem
que os buckets de dados, backups e state continuam acessiveis.

Um apply seletivo pode manter outputs antigos de EC2/IP no state. Esses outputs
nao comprovam que a VM continua existindo: consulte a API EC2 e os recursos
gerenciados restantes. Um novo plan normal pode propor recriar o runtime;
nao aplique essa recriacao durante a remocao.

Remover as EC2 encerra o acesso ao Nextcloud e Grafana. Os buckets preservados
continuam sujeitos a cobranca de armazenamento. Recriar o ambiente usa o deploy
e um restore separados; nao ocorre automaticamente por este procedimento.
