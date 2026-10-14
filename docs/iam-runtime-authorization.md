> Registro sanitizado: ACCOUNT_ID, PUBLIC_IPV4, ADMIN_IPV4, RESOURCE_ID e lineages
> são placeholders; valores reais permanecem somente em registros privados.
> Estado atual e limites em [Fase 6](phase-6.md).

# Revisão IAM EC2 após o primeiro deploy — 2026-10-07

Status: apply exclusivo do plano salvo autorizado e concluído em 2026-10-07: 0 added, 1 changed, 0 destroyed. A policy ativa tem 19 statements e corresponde exatamente à proposta revisada. Nenhum workflow adicional ou destroy foi executado. Runtime, persistent, OIDC, boundary, providers e Secrets não foram alterados. Persistent aplicado (12 recursos) e runtime parcial (6 recursos) foram preservados.

## Causa raiz e escopo da correção

O apply do run <RUN_ID> recebeu UnauthorizedOperation em CreateSubnet, CreateRouteTable e CreateSecurityGroup na VPC RESOURCE_ID. CreateTaggedRuntime já incluía ARN vpc/*, mas aplicava RequestTag/Project e RequestTag/Environment a todos os recursos autorizados. Para as três operações, a avaliação da VPC existente usa ResourceTag; as request tags pertencem ao recurso novo. A presença do ARN, sem condição compatível, não concedia a autorização dependente.

CreateTaggedRuntime foi preservado integralmente no JSON, incluindo CreateVpc, os seis ARNs regionais/account e ambas as RequestTag. Somente seu comentário foi corrigido. A nova autorização CreateResourcesInTaggedVpc concede as três ações exclusivamente em vpc/* da conta/região configuradas, com ResourceTag/Project e ResourceTag/Environment obrigatórias. Não fixa o ID da VPC nem concede ações gerais de EC2.

A auditoria identificou uma segunda incompatibilidade comprovada na tabela oficial atual: AuthorizeSecurityGroupIngress/Egress também avaliam security-group-rule quando TagSpecifications está presente. Terraform envia as default tags nas regras e o controlador envia tags na regra temporária. ManageTaggedRuntime cobre o SG pai com ResourceTag, mas sua condição ResourceTag não cobre a regra ainda inexistente. CreateTaggedSecurityGroupRules autoriza somente esses dois actions sobre security-group-rule/* da conta/região, com as mesmas duas RequestTag. Não há nova permissão para alterar SGs sem as tags do projeto.

## Referências oficiais

- [Tabela EC2: actions, resource types e condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html)
- [Como interpretar resources obrigatórios e condições por recurso](https://docs.aws.amazon.com/service-authorization/latest/reference/reference_policies_actions-resources-contextkeys.html)
- [Autorização adicional CreateTags durante criação](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html)

## Policy anterior relevante, renderizada para lab

```json
{
  "Action": [
    "ec2:ImportKeyPair",
    "ec2:CreateVpc",
    "ec2:CreateSubnet",
    "ec2:CreateSecurityGroup",
    "ec2:CreateRouteTable",
    "ec2:CreateInternetGateway"
  ],
  "Condition": {
    "StringEquals": {
      "aws:RequestTag/Environment": "lab",
      "aws:RequestTag/Project": "nextcloud-platform"
    }
  },
  "Effect": "Allow",
  "Resource": [
    "arn:aws:ec2:us-east-1:ACCOUNT_ID:vpc/*",
    "arn:aws:ec2:us-east-1:ACCOUNT_ID:subnet/*",
    "arn:aws:ec2:us-east-1:ACCOUNT_ID:security-group/*",
    "arn:aws:ec2:us-east-1:ACCOUNT_ID:route-table/*",
    "arn:aws:ec2:us-east-1:ACCOUNT_ID:key-pair/*",
    "arn:aws:ec2:us-east-1:ACCOUNT_ID:internet-gateway/*"
  ],
  "Sid": "CreateTaggedRuntime"
}
```

## Dois statements adicionados, renderizados para lab

```json
[
  {
    "Action": [
      "ec2:CreateSubnet",
      "ec2:CreateSecurityGroup",
      "ec2:CreateRouteTable"
    ],
    "Condition": {
      "StringEquals": {
        "aws:ResourceTag/Environment": "lab",
        "aws:ResourceTag/Project": "nextcloud-platform"
      }
    },
    "Effect": "Allow",
    "Resource": "arn:aws:ec2:us-east-1:ACCOUNT_ID:vpc/*",
    "Sid": "CreateResourcesInTaggedVpc"
  },
  {
    "Action": [
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress"
    ],
    "Condition": {
      "StringEquals": {
        "aws:RequestTag/Environment": "lab",
        "aws:RequestTag/Project": "nextcloud-platform"
      }
    },
    "Effect": "Allow",
    "Resource": "arn:aws:ec2:us-east-1:ACCOUNT_ID:security-group-rule/*",
    "Sid": "CreateTaggedSecurityGroupRules"
  }
]
```

## Diff IAM completo

```diff
diff --git a/terraform/aws/bootstrap/operational-iam.tf b/terraform/aws/bootstrap/operational-iam.tf
index 1ad98fc..d4cac9f 100644
--- a/terraform/aws/bootstrap/operational-iam.tf
+++ b/terraform/aws/bootstrap/operational-iam.tf
@@ -93,7 +93,7 @@ data "aws_iam_policy_document" "operational" {
   statement {
     sid     = "CreateTaggedRuntime"
     actions = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:ImportKeyPair"]
-    # APIs de criação autorizam também recursos pai; a região/conta é o limite.
+    # RequestTag vale para os recursos novos. A VPC dependente é autorizada abaixo com ResourceTag.
     resources = ["${local.ec2_arn}:vpc/*", "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:internet-gateway/*", "${local.ec2_arn}:route-table/*", "${local.ec2_arn}:security-group/*", "${local.ec2_arn}:key-pair/*"]
     condition {
       test     = "StringEquals"
@@ -106,6 +106,37 @@ data "aws_iam_policy_document" "operational" {
       values   = [var.environment]
     }
   }
+  statement {
+    sid       = "CreateResourcesInTaggedVpc"
+    actions   = ["ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup"]
+    resources = ["${local.ec2_arn}:vpc/*"]
+    condition {
+      test     = "StringEquals"
+      variable = "aws:ResourceTag/Project"
+      values   = [var.project_name]
+    }
+    condition {
+      test     = "StringEquals"
+      variable = "aws:ResourceTag/Environment"
+      values   = [var.environment]
+    }
+  }
+  statement {
+    # TagSpecifications exige autorização da regra nova, além do SG existente em ManageTaggedRuntime.
+    sid       = "CreateTaggedSecurityGroupRules"
+    actions   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"]
+    resources = ["${local.ec2_arn}:security-group-rule/*"]
+    condition {
+      test     = "StringEquals"
+      variable = "aws:RequestTag/Project"
+      values   = [var.project_name]
+    }
+    condition {
+      test     = "StringEquals"
+      variable = "aws:RequestTag/Environment"
+      values   = [var.environment]
+    }
+  }
   statement {
     sid       = "RunTaggedInstanceAndVolume"
     actions   = ["ec2:RunInstances"]
```

## Auditoria de todas as ações EC2 existentes

A tabela abaixo cobre todas as ações EC2 presentes na policy proposta. Mostra somente os resource types usados pela configuração atual. Tipos opcionais para IPAM, Spot, GPU, license configuration, placement group, launch template, pools, VPN e prefix lists não estão configurados e não receberam permissões adicionais. RequestTag/ResourceTag sempre significam Project e Environment com os valores parametrizados.

RunInstances já cobre image, instance, volume, network-interface, subnet, security-group e key-pair. Instance/volume exigem RequestTag; o volume raiz planejado também recebe as default tags. A ENI implícita não tem tags explícitas no runtime e mantém a autorização regional/account existente, sem exigir tags inexistentes. Não foi acrescentado CreateNetworkInterface, AttachVolume, snapshot ou KMS: não foi demonstrada chamada independente necessária na configuração atual.

AssociateIamInstanceProfile e RunInstances dependem também de iam:PassRole, que continua restrito à role EC2 exata e iam:PassedToService=ec2.amazonaws.com. AttachInternetGateway cobre tanto IGW quanto VPC; AssociateRouteTable cobre route-table e subnet. CreateRoute autoriza route-table; não requer autorização CreateRoute sobre o IGW destino.

| Ação EC2 | Resources utilizados / condição | Resultado |
| --- | --- | --- |
| `AssociateIamInstanceProfile` | instance; ResourceTag | Cobertura existente compatível |
| `AssociateRouteTable` | internet-gateway, route-table, subnet; ResourceTag | Cobertura existente compatível |
| `AttachInternetGateway` | internet-gateway, vpc; ResourceTag | Cobertura existente compatível |
| `AuthorizeSecurityGroupEgress` | security-group=ResourceTag; security-group-rule nova=RequestTag | Regra nova corrigida |
| `AuthorizeSecurityGroupIngress` | security-group=ResourceTag; security-group-rule nova=RequestTag | Regra nova corrigida |
| `CreateInternetGateway` | internet-gateway; RequestTag | Preservada |
| `CreateRoute` | route-table; ResourceTag | Cobertura existente compatível |
| `CreateRouteTable` | route-table, vpc; novo=RequestTag, VPC=ResourceTag | VPC corrigida |
| `CreateSecurityGroup` | security-group, vpc; novo=RequestTag, VPC=ResourceTag | VPC corrigida |
| `CreateSubnet` | subnet, vpc; novo=RequestTag, VPC=ResourceTag | VPC corrigida |
| `CreateTags` | ARN regional/account; ec2:CreateAction na criação; ResourceTag em atualização | Preservada |
| `CreateVpc` | vpc; RequestTag | Preservada |
| `DeleteInternetGateway` | internet-gateway; ResourceTag | Cobertura existente compatível |
| `DeleteKeyPair` | key-pair; ResourceTag | Cobertura existente compatível |
| `DeleteRoute` | route-table; ResourceTag | Cobertura existente compatível |
| `DeleteRouteTable` | route-table; ResourceTag | Cobertura existente compatível |
| `DeleteSecurityGroup` | security-group; ResourceTag | Cobertura existente compatível |
| `DeleteSubnet` | subnet; ResourceTag | Cobertura existente compatível |
| `DeleteTags` | image, instance, internet-gateway, key-pair, network-interface, route-table, security-group, security-group-rule, subnet, volume, vpc; ResourceTag | Cobertura existente compatível |
| `DeleteVpc` | vpc; ResourceTag | Cobertura existente compatível |
| `DescribeAvailabilityZones` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeIamInstanceProfileAssociations` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeImages` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeInstanceAttribute` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeInstanceCreditSpecifications` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeInstanceStatus` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeInstanceTypes` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeInstances` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeInternetGateways` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeKeyPairs` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeNetworkInterfaces` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeRouteTables` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeSecurityGroupRules` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeSecurityGroups` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeSubnets` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeTags` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeVolumes` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeVolumesModifications` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeVpcAttribute` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DescribeVpcs` | *; aws:RequestedRegion (leitura regional existente) | Preservada |
| `DetachInternetGateway` | internet-gateway, vpc; ResourceTag | Cobertura existente compatível |
| `DisassociateIamInstanceProfile` | instance; ResourceTag | Cobertura existente compatível |
| `DisassociateRouteTable` | internet-gateway, route-table, subnet; ResourceTag | Cobertura existente compatível |
| `GetConsoleOutput` | instance; ResourceTag | Cobertura existente compatível |
| `ImportKeyPair` | key-pair; RequestTag | Preservada |
| `ModifyInstanceAttribute` | instance, security-group, volume; ResourceTag | Cobertura existente compatível |
| `ModifyInstanceCreditSpecification` | instance; ResourceTag | Cobertura existente compatível |
| `ModifyInstanceMetadataOptions` | instance; ResourceTag | Cobertura existente compatível |
| `ModifySecurityGroupRules` | security-group, security-group-rule; ResourceTag | Cobertura existente compatível |
| `ModifySubnetAttribute` | subnet; ResourceTag | Cobertura existente compatível |
| `ModifyVolume` | volume; ResourceTag | Cobertura existente compatível |
| `ModifyVpcAttribute` | vpc; ResourceTag | Cobertura existente compatível |
| `ReplaceIamInstanceProfileAssociation` | instance; ResourceTag | Cobertura existente compatível |
| `ReplaceRoute` | route-table; ResourceTag | Cobertura existente compatível |
| `ReplaceRouteTableAssociation` | internet-gateway, route-table, subnet; ResourceTag | Cobertura existente compatível |
| `RevokeSecurityGroupEgress` | security-group; ResourceTag | Cobertura existente compatível |
| `RevokeSecurityGroupIngress` | security-group; ResourceTag | Cobertura existente compatível |
| `RunInstances` | image, instance, key-pair, network-interface, security-group, subnet, volume; RequestTag em instance/volume, dependências regionais | Cobertura existente completa para este launch |
| `StartInstances` | instance; ResourceTag | Cobertura existente compatível |
| `StopInstances` | instance; ResourceTag | Cobertura existente compatível |
| `TerminateInstances` | instance; ResourceTag | Cobertura existente compatível |

A auditoria verifica compatibilidade com o modelo oficial para as chamadas atuais. A simulação IAM verifica os contextos informados; ela não reproduz integralmente o serviço EC2, políticas organizacionais ou futuras chamadas do provider. Nenhum sucesso de um apply futuro é presumido.

## Validação e plan real

- Terraform 1.14.2; hashicorp/aws permanece 6.67.0.
- terraform fmt terraform/aws/bootstrap e fmt -check -recursive terraform: sucesso.
- terraform -chdir=terraform/aws/bootstrap validate: sucesso.
- terraform -chdir=terraform/aws/bootstrap test: 6 passaram, zero falhas; mock_provider e command=plan, sem recursos reais.
- check_iam.py: 19 statements, 8.734 caracteres com nomes máximos, abaixo do limite inline de 10.240.
- unittest do controlador: 10 passaram.
- check_static.py: passou em cópia dos arquivos rastreados, sem os arquivos backend.tf locais ignorados; o teste exige checkout ainda não ativado, enquanto bootstrap local já usa S3. Nenhum backend real foi desativado.
- IAM SimulateCustomPolicy: 18 cenários passaram. Policy anterior nega a VPC sem RequestTag e a regra nova sem ResourceTag; proposta autoriza os contextos corretos. Tags ausentes/incorretas, conta/região diferentes e CreateVpc sem tags continuam negados. Nenhuma policy AWS foi atualizada.
- Plan com backend S3 ativo, locking habilitado, -lock-timeout=5m e environments/lab/bootstrap.tfvars: **0 to add, 1 to change, 0 to destroy**.
- Única alteração: aws_iam_role_policy.operational[0], update in-place da policy persistent-runtime-deploy da role GitHub.
- Inspeção JSON do plan confirmou os 17 statements anteriores intactos, exatamente os dois additions e outputs inalterados. OIDC, boundary, backup policy e bucket de state continuam sem mudanças.

Plano salvo em .validation-tools/iam-policy-review/bootstrap-iam-fix.tfplan, ignorado pelo Git e com ACL restrita ao usuário, Administradores e SYSTEM. Aplicação desse plano foi autorizada separadamente e concluída; nenhuma repetição do deploy foi executada. Registro referente à validação do bootstrap antes da retomada do deploy.

## Validação após o apply autorizado

- SHA256 do plano salvo conferido antes do apply: `4ac4a4e6d7a034f2e0664e42bd18368ded8f6fa57b45c6990e25c31e407885f1`; o plano não foi regenerado.
- Apply: **0 added, 1 changed, 0 destroyed**, somente `aws_iam_role_policy.operational[0]`.
- `aws iam get-role-policy`: policy `persistent-runtime-deploy` da role `nextcloud-platform-lab-github` idêntica à proposta revisada; os 17 statements anteriores permanecem intactos.
- Novos statements exatamente `CreateResourcesInTaggedVpc` e `CreateTaggedSecurityGroupRules`.
- Outputs do bootstrap preservados.
- Plan posterior com os mesmos tfvars, backend S3 e locking habilitado: **0 to add, 0 to change, 0 to destroy**, exit code 0 (`-detailed-exitcode`).
- Nenhum persistent/runtime apply, workflow, destroy ou Fase 7 executado.
