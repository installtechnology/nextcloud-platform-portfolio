# Grafana: acesso IPv4 e destino dinâmico

Correção publicada e aplicada no laboratório em 8 de outubro de 2026, com uma
execução automatizada bem-sucedida. Este registro é sanitizado; IPs e IDs reais
não integram a documentação de portfólio. [Resultado completo](phase-6.md).

## Causa confirmada

Docker Engine 29.8.2 / Compose 5.6.0: Grafana healthy e API interna HTTP 200,
mas a publicação solicitada em loopback não era efetiva: 3000/tcp=null e
Connection refused pelo host. Prometheus/Loki continuavam alcançáveis pela bridge.

Uma reprodução isolada no mesmo daemon confirmou binding nulo quando ligado
somente à bridge internal e publicação funcional quando ligado também a uma
bridge não interna. Fixtures usaram imagem presente, sem secrets/volumes reais,
somente portas temporárias loopback; foram removidas ao término. O diagnóstico
não atribuiu a falha ao Grafana, às credenciais ou aos datasources.

## Solução aplicada

- Preservar observability internal e adicionar grafana_access somente ao Grafana,
  bridge não interna com IPv6 desabilitado; não utilizar network_mode host.
- Publicar explicitamente 0.0.0.0:3000:3000, somente IPv4.
- Autorizar TCP/3000 no SG somente para HTTP_ALLOWED_CIDRS administrativos;
  Terraform e controlador rejeitam /0 e IPv6, sem IP de origem fixado no código.
- Renderizar GRAFANA_PUBLIC_HOST a partir de ansible_host; GF_SERVER_ROOT_URL
  acompanha o IP atual. Outputs/API geram inventário e URLs de ambos os serviços.
- Ansible verifica binding efetivo, /api/health no host, datasources autenticados
  e dashboard provisionado. Coleta, dashboards e autenticação permanecem iguais.

## Resultado e preservação

| Etapa | Persistent | Runtime |
| --- | --- | --- |
| Plan/apply da correção | 0/0/0 | 1/0/0: ingress Grafana |
| Plan posterior | 0/0/0 | 0/0/0 |

EC2/EBS, IAM, buckets, VPC e subnet preservados. O state runtime passou de 15
para 16 recursos; persistent manteve 12. Só Grafana foi recriado; os outros sete
containers e os seis volumes continuaram os mesmos, incluindo grafana_data.
Nextcloud continuou installed=true e healthy, sem reinstalação.

Grafana: binding IPv4 efetivo, host HTTP 200 e database=ok. Datasources
Prometheus/Loki e dashboard confirmados por API autenticada; API privada retornou
401 sem autenticação. Anônimo/signup desabilitados. Cleanup da regra SSH
temporária do runner confirmado na AWS. Nenhuma regra TCP/3000 para o runner.

## Segurança e limites da evidência

0.0.0.0 não é controle de acesso: proteção externa depende do SG restritivo.
Na revisão foram conferidos SG único, ausência de ingress IPv6/caminhos de
origem alternativos, ENI sem IPv6 e daemon sem allow-direct-routing. Nova bridge
somente Grafana, collectors sem portas externas. Processos do host/peers internos
podem acessar o serviço; autenticação continua necessária. Grafana ganha egress
pela nova bridge; não afirmar isolamento de IMDS ou ampliar IAM.

Testes HTTP automatizados finais aconteceram dentro da EC2. Separadamente,
o operador relatou acesso direto externo sem túnel, login administrativo,
dashboard platform-overview, conexões Prometheus/Loki e visualização de métricas
e logs, todos OK. Trata-se de validação manual relatada pelo operador, não de
teste GitHub Actions; não foram fornecidos screenshots ou evidências técnicas
adicionais. HTTP sem TLS não criptografa login/sessão; CIDR não substitui HTTPS.

Testes aprovados: fmt/validate/plans, Terraform mock CIDRs/IPv6, Compose config,
estáticos, syntax-check/lint, URLs/inventário com dois IPs diferentes e regressões
guard/instalador. Inventário/URLs dinâmicos foram exercitados com mocks; não foi
executado Destroy/DR para provar recriação operacional nesta fase.

## Procedimento para próximas alterações

Revisar código e autorização, confirmar origem administrativa e identidade da
instância atual, executar plans e interromper diante de mudanças inesperadas.
O workflow aplica Terraform e depois Ansible/Compose; não manter patch manual
exclusivo na EC2. Revalidar binding, login, datasources, dashboard, healthchecks
e cleanup. Testar acesso externo a partir de cliente autorizado, sem ampliar
SG ao runner. Falhas preservam recursos/dados para diagnóstico.

Fontes: [Docker bridge networking](https://docs.docker.com/engine/network/drivers/bridge/),
[Compose networks/internal](https://docs.docker.com/reference/compose-file/networks/),
[Moby: publicação em rede internal](https://github.com/moby/moby/discussions/53256).
