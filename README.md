# caswork-licitacoes

Busca automática, semanal, de licitações abertas no PNCP (Portal Nacional de
Contratações Públicas) em duas categorias:

1. **Equipamento importado** — editais que combinam um segmento de interesse
   (automação industrial; energia — transformadores, geradores, armazenamento
   de energia/BESS; óleo & gás) **com** um sinal explícito de importação no
   objeto ("importado", "fabricação estrangeira", "licitação internacional"
   etc). As duas condições são exigidas juntas para evitar ruído — muitos
   editais de obra civil citam "gerador" ou "subestação" de passagem sem ter
   nenhuma relação com equipamento importado.
2. **Comércio exterior / logística internacional** — editais em que o órgão
   quer *contratar* esse tipo de serviço diretamente (despachante aduaneiro,
   agente de carga, operador logístico internacional, assessoria em comércio
   exterior etc.) — oportunidade de negócio direta pra Caswork, não depende
   de sinal de importação adicional.

Filtra São Paulo e nacional, e publica os achados em dois canais do Slack:

- **#caswork-licitacoes** — captação: licitações novas encontradas a cada execução.
- **#followup-licitacoes** — acompanhamento: alerta quando o prazo de proposta
  de uma licitação já captada está a 7 dias ou menos do encerramento.

## Como funciona

- `.github/workflows/pncp-licitacoes.yml` roda toda segunda-feira às 09:00
  (horário de Brasília) via GitHub Actions, ou sob demanda (`workflow_dispatch`).
- `scripts/pncp_licitacoes.py` consulta a API pública de Consulta do PNCP:
  `/v1/contratacoes/proposta` para modalidades competitivas (Concorrência,
  Pregão, Diálogo Competitivo — têm janela de proposta) e
  `/v1/contratacoes/publicacao` para Dispensa/Inexigibilidade (contratação
  direta, sem fase de proposta — usa "publicado nos últimos 14 dias" como
  substituto de "aberto"). Filtra por palavras-chave de segmento e de sinal
  de importação, e posta no Slack via `chat.postMessage`.
- `data/licitacoes_estado.json` guarda o que já foi notificado (evita
  duplicidade) e as datas de encerramento, para gerar os alertas de follow-up.
  É atualizado e commitado pelo próprio workflow a cada execução.

## Configuração necessária (uma vez)

O workflow roda em runners do GitHub, que são independentes de qualquer
conexão Slack de uma sessão do Claude — por isso ele precisa das próprias
credenciais, configuradas em **Settings → Secrets and variables → Actions**
deste repositório:

### Secret
- `SLACK_BOT_TOKEN`: token de bot de um Slack App com escopo `chat:write`,
  instalado no workspace e convidado (`/invite @nome-do-bot`) nos canais
  `#caswork-licitacoes` e `#followup-licitacoes`.
  Criar em https://api.slack.com/apps → "Create New App" → "OAuth & Permissions"
  → adicionar escopo `chat:write` → "Install to Workspace" → copiar o
  "Bot User OAuth Token" (começa com `xoxb-`).

### Variables
- `SLACK_CHANNEL_CAPTACAO`: `C0C2W9FRC1Y` (#caswork-licitacoes)
- `SLACK_CHANNEL_FOLLOWUP`: `C0C2YA6R7DX` (#followup-licitacoes)

Depois de configurar os dois itens acima, rode o workflow manualmente uma vez
(aba **Actions** → "Busca semanal de licitações PNCP" → **Run workflow**) para
validar antes de deixar no automático.

## Ajustando os filtros

As listas de palavras-chave ficam no topo de `scripts/pncp_licitacoes.py`:
`SEGMENT_KEYWORDS` + `IMPORT_SIGNAL_KEYWORDS` (categoria "equipamento
importado") e `COMEX_KEYWORDS` (categoria "comércio exterior/logística
internacional"). Ajuste/adicione termos conforme os resultados forem
aparecendo — a API do PNCP não permite busca por palavra-chave no lado do
servidor, então o filtro é sempre feito no texto do objeto após a consulta,
usando borda de palavra (`\b`) pra evitar falsos positivos por substring
(ex: "scada" não deve casar dentro de "escadarias").
