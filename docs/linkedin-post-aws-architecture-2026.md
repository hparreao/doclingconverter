# De um Streamlit deployado a um conversor documental com limites explícitos de custo

Durante algum tempo, este conversor de documentos era um Streamlit deployado. Era suficiente para validar a experiência: subir um arquivo, usar o Docling e baixar Markdown ou JSON. A mesma simplicidade concentrava interface, upload, processamento e retenção no mesmo runtime. Isso deixa de ser uma abstração aceitável quando o endpoint passa a ser público.

A migração de 2026 separou o plano de controle do plano de dados.

O site estático solicita à Lambda uma política S3 curta para um único upload. O navegador envia o arquivo diretamente para um bucket privado; a API não recebe o conteúdo do documento. Depois do upload, outra chamada submete o job. Um worker Lambda executa o Docling de forma assíncrona e grava o resultado temporário. DynamoDB mantém estado, cotas e um lock condicional que aceita somente uma conversão ativa por vez.

Essa última escolha não é uma tentativa de transformar Lambda em fila. Ela estabelece um limite explícito de custo e de concorrência em uma conta com quota regional restrita. O worker x86_64 tem 3008 MB, armazenamento efêmero de 5 GB e timeout de 10 minutos. O import pesado do Docling ocorre apenas depois da admissão do job, evitando pagar esse custo para requisições rejeitadas.

Também deixei os guardrails visíveis no contrato público: um arquivo de até 25 MB; cinco criações por IP por dia; no máximo 100 conversões por mês; 60 requisições por IP por minuto; teto global de 3.000 requisições por dia e 20.000 por mês. As cotas de criação de job são atualizadas em uma transação DynamoDB, portanto uma tentativa negada não consome uma vaga da cota global. A política assíncrona não repete automaticamente jobs que falharam.

Privacidade e retenção também foram tratadas como comportamento de infraestrutura. O bucket bloqueia acesso público e usa criptografia em repouso. O arquivo de origem é removido quando o worker termina. Objetos remanescentes e o estado do job expiram em até 24 horas. O resultado só é exposto por URL pré-assinada de curta duração.

O pipeline de deploy usa GitHub Actions com OIDC, sem chaves AWS no repositório. A imagem Docker imutável é publicada no ECR e o CloudFormation atualiza as funções. Mantive o Docker como opção de self-hosting com `docling-serve`; o Streamlit original permanece deprecado no histórico, não como runtime recomendado.

Há limites importantes para não vender uma arquitetura imaginária: a Function URL é pública e CORS não é autenticação. Os limites atuais reduzem superfície de abuso e deixam o custo de conversão finito, mas não substituem WAF, autenticação ou uma arquitetura multi-tenant. Esses seriam os próximos degraus caso o uso e o risco justificassem o custo operacional.

O conversor está disponível em https://aiforwarddeployedengineer.com/converter/

O código e a infraestrutura estão em https://github.com/hparreao/doclingconverter

#AWS #Lambda #Docling #OCR #CloudArchitecture #Serverless #MLOps

## Legenda sugerida para o diagrama

Do browser ao resultado temporário: uploads diretos ao S3 privado, controle de estado e cota no DynamoDB, conversão serializada com Docling e deploy por OIDC.
