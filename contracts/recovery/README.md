# Contratos de recuperação v1

O formato vigente para novos backups operacionais é **v2 SSE-S3**, conforme
[contrato e decisão autorizados](../../docs/phase-7-pr7-sse-s3.md).
Manifesto/completion/evidência v1 abaixo permanecem para compatibilidade histórica;
o inventário v1 é compartilhado. Não misturar v1/v2 no mesmo conjunto.

Somente PR1: especificação e testes de exemplos, sem produtor, verificador de
artefatos, CLI, AWS, backup ou restore. Normas: [Fase 7](../../docs/phase-7.md).

| Schema | Documento |
| --- | --- |
| [manifest](schemas/manifest-v1.schema.json) | Conjunto completo/íntegro, sempre com restore_proven=false |
| [object-inventory](schemas/object-inventory-v1.schema.json) | Objetos visíveis/versionados e ausências no ponto quiescente |
| [completion](schemas/completion-v1.schema.json) | Publicado por último; vincula versão/hash do manifesto criptografado |
| [restore-evidence](schemas/restore-evidence-v1.schema.json) | Evidência funcional separada, vinculada ao mesmo conjunto em destino isolado |

JSON Schema Draft 2020-12; validadores precisam habilitar `format` como assertion
(nos testes: `FormatChecker`). Sem refs remotas. Campos extras são rejeitados.
Cada objeto é fechado, mas isso não substitui inspeção de valores nem autenticidade.
Regras entre documentos, acesso a versões e hash dos bytes não são JSON Schema.

## Fixtures

**Todos os exemplos são fictícios, não backups e não evidências.** Conta zeros,
buckets fixture, hashes/digests repetidos e VersionIds inventados. Nunca usar como
inputs operacionais. `restore-evidence.json` ilustra somente a forma de uma prova;
nenhum restore real foi executado. Nenhuma senha ou arquivo de aplicação é incluído.

`rejection-cases.json` contém mutações de documento por JSON Pointer simplificado,
com indicação da camada esperada. Os testes exercitam conjuntos incompletos,
misturados, incompatíveis, inventário truncado, ausência conflitante e destino original.
Casos semânticos exigem o código de erro exato; casos estruturais exigem a keyword
JSON Schema esperada. additional_mutations pode manter uma pré-condição (como
object_count correto) para isolar o erro alvo. Não basta falhar por um motivo qualquer.
O oráculo semântico vive somente no teste: não é biblioteca de produção ou PR2.
Não lê arquivos reais de backup, calcula hashes, restaura banco ou abre conexões.

## Executar localmente

Na raiz do repositório, usando Python 3.11 ou superior:

```text
python -m venv .venv
python -m pip install -r contracts/recovery/tests/requirements.txt
python -m unittest discover -s contracts/recovery/tests -p "test_*.py" -v
git diff --check
```

Ative a `.venv` antes do pip/testes (`.venv\Scripts\Activate.ps1` em PowerShell,
`source .venv/bin/activate` em Linux). Dependência exclusiva dos testes, não do deploy.
O runner de testes valida meta-schemas, exemplos positivos e negativos, relações
normativas e formatos, sem buscar refs na rede. Aprovação dos testes não demonstra
captura consistente real, criptografia, integridade de conteúdo ou recuperação funcional.

As quatro definições são autocontidas: não há referências externas entre schemas.
As relações entre documentos são normativas e exercitadas pelo oráculo de testes;
os tipos compartilhados são comparados para evitar drift. Regex usa fim absoluto
compatível com ECMAScript, sem aceitar newline final em hashes/identificadores.
Versões S3 são opacas; presença/formato não comprova que existam no serviço.
Manifesto/marker/evidência não oferecem aprovação de Destroy nem uma API de elegibilidade.
