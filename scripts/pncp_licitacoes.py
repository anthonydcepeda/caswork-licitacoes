#!/usr/bin/env python3
"""Busca licitações abertas no PNCP com potencial de equipamento importado.

Consulta a API pública de Consulta do PNCP (https://pncp.gov.br/api/consulta),
filtra editais cujo objeto sinaliza os segmentos de interesse (automação
industrial, energia/BESS, óleo & gás) combinados com sinais de importação, e
publica os achados em canais do Slack:

- Achados novos (ainda não notificados)  -> canal de captação
- Prazos de proposta se aproximando de licitações já captadas -> canal de follow-up

O estado (o que já foi notificado, e quando) fica persistido em
`data/licitacoes_estado.json`, versionado no repositório pelo próprio workflow.

Referência da API: https://pncp.gov.br/api/consulta/swagger-ui/index.html
Os nomes de campo abaixo seguem o schema documentado publicamente; como este
script roda fora de um ambiente com acesso à internet no momento em que foi
escrito, rode uma vez via `workflow_dispatch` após configurar os segredos do
Slack para confirmar que o schema não mudou.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import requests

PNCP_BASE_URL = "https://pncp.gov.br/api/consulta/v1"
PNCP_EDITAL_URL = "https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}"
SLACK_API_URL = "https://slack.com/api/chat.postMessage"

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "licitacoes_estado.json"

REQUEST_TIMEOUT = 30
PAGE_SIZE = 50
MAX_PAGES_POR_MODALIDADE = 20
LOOKAHEAD_DIAS_PROPOSTA = 120  # janela de busca de prazos de encerramento
FOLLOWUP_ALERTA_DIAS = 7  # avisa no follow-up quando faltar <= N dias para o prazo

# Modalidades relevantes para compra de equipamentos de médio/alto valor.
MODALIDADES = {
    2: "Diálogo Competitivo",
    4: "Concorrência Eletrônica",
    5: "Concorrência Presencial",
    6: "Pregão Eletrônico",
    7: "Pregão Presencial",
}

SEGMENT_KEYWORDS: dict[str, list[str]] = {
    "Automação industrial": [
        "automacao industrial",
        "automacao de processos",
        "sistema de automacao",
        "controlador logico programavel",
        "clp",
        "scada",
        "instrumentacao industrial",
        "robotica industrial",
        "dcs",
        "sistema de controle distribuido",
        "ihm",
        "interface homem-maquina",
    ],
    "Energia (transformadores/geradores/BESS)": [
        "transformador",
        "gerador",
        "subestacao",
        "armazenamento de energia",
        "bess",
        "battery energy storage",
        "banco de baterias",
        "turbina",
        "usina termeletrica",
        "usina hidreletrica",
        "grupo gerador",
        "nobreak",
        "no-break",
    ],
    "Óleo & gás": [
        "oleo e gas",
        "upstream",
        "plataforma de petroleo",
        "perfuracao",
        "exploracao e producao",
        "sonda de perfuracao",
        "unidade de processamento de gas",
        "gasoduto",
        "compressor de gas",
        "separador trifasico",
        "arvore de natal",
        "fpso",
    ],
}

IMPORT_SIGNAL_KEYWORDS = [
    "importad",  # importado/importados/importada/importadas
    "importacao",
    "fabricacao estrangeira",
    "licitacao internacional",
    "procedencia estrangeira",
    "origem estrangeira",
    "fabricante estrangeiro",
    "desembaraco aduaneiro",
    "nacionalizacao aduaneira",
]


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acento.lower()


def classificar_objeto(objeto: str) -> tuple[list[str], bool]:
    norm = normalizar(objeto)
    segmentos = [nome for nome, termos in SEGMENT_KEYWORDS.items() if any(t in norm for t in termos)]
    sinal_importacao = any(t in norm for t in IMPORT_SIGNAL_KEYWORDS)
    return segmentos, sinal_importacao


@dataclass
class Licitacao:
    numero_controle: str
    orgao: str
    uf: str
    municipio: str
    objeto: str
    valor_estimado: float | None
    modalidade: str
    abertura_proposta: str | None
    encerramento_proposta: str | None
    link: str
    segmentos: list[str] = field(default_factory=list)
    sinal_importacao: bool = False


def montar_link(item: dict[str, Any]) -> str:
    orgao = item.get("orgaoEntidade") or {}
    cnpj = orgao.get("cnpj")
    ano = item.get("anoCompra")
    sequencial = item.get("sequencialCompra")
    if cnpj and ano and sequencial:
        return PNCP_EDITAL_URL.format(cnpj=cnpj, ano=ano, sequencial=sequencial)
    return f"https://pncp.gov.br/app/editais/{item.get('numeroControlePNCP', '')}"


def parse_item(item: dict[str, Any]) -> Licitacao | None:
    objeto = item.get("objetoCompra") or ""
    segmentos, sinal_importacao = classificar_objeto(objeto)
    if not segmentos:
        return None

    orgao = item.get("orgaoEntidade") or {}
    unidade = item.get("unidadeOrgao") or {}
    numero_controle = item.get("numeroControlePNCP") or ""
    if not numero_controle:
        return None

    return Licitacao(
        numero_controle=numero_controle,
        orgao=orgao.get("razaoSocial") or unidade.get("nomeUnidade") or "(órgão não informado)",
        uf=unidade.get("ufSigla") or "",
        municipio=unidade.get("municipioNome") or "",
        objeto=objeto.strip(),
        valor_estimado=item.get("valorTotalEstimado"),
        modalidade=item.get("modalidadeNome") or MODALIDADES.get(item.get("codigoModalidadeContratacao"), ""),
        abertura_proposta=item.get("dataAberturaProposta"),
        encerramento_proposta=item.get("dataEncerramentoProposta"),
        link=montar_link(item),
        segmentos=segmentos,
        sinal_importacao=sinal_importacao,
    )


def buscar_modalidade(session: requests.Session, codigo_modalidade: int, data_final: str) -> Iterator[dict[str, Any]]:
    pagina = 1
    while pagina <= MAX_PAGES_POR_MODALIDADE:
        params = {
            "codigoModalidadeContratacao": codigo_modalidade,
            "dataFinal": data_final,
            "pagina": pagina,
            "tamanhoPagina": PAGE_SIZE,
        }
        resp = session.get(f"{PNCP_BASE_URL}/contratacoes/proposta", params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 204:
            return
        resp.raise_for_status()
        payload = resp.json()
        dados = payload.get("data") or []
        if not dados:
            return
        yield from dados

        total_paginas = payload.get("totalPaginas") or pagina
        if pagina >= total_paginas:
            return
        pagina += 1
        time.sleep(0.3)


def buscar_licitacoes_abertas() -> list[Licitacao]:
    hoje = date.today()
    data_final = (hoje + timedelta(days=LOOKAHEAD_DIAS_PROPOSTA)).strftime("%Y%m%d")

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    encontradas: dict[str, Licitacao] = {}
    for codigo_modalidade in MODALIDADES:
        try:
            for item in buscar_modalidade(session, codigo_modalidade, data_final):
                licitacao = parse_item(item)
                if licitacao is None:
                    continue
                # confirma que a proposta ainda está aberta (defensivo: a
                # semântica exata do filtro dataFinal da API pode variar).
                if licitacao.encerramento_proposta:
                    try:
                        encerramento = datetime.fromisoformat(licitacao.encerramento_proposta).date()
                        if encerramento < hoje:
                            continue
                    except ValueError:
                        pass
                encontradas[licitacao.numero_controle] = licitacao
        except requests.RequestException as exc:
            print(f"[aviso] falha ao consultar modalidade {codigo_modalidade}: {exc}", file=sys.stderr)

    return list(encontradas.values())


def carregar_estado() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
    return {}


def salvar_estado(estado: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def formatar_valor(valor: float | None) -> str:
    if valor is None:
        return "não informado"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_data(data_iso: str | None) -> str:
    if not data_iso:
        return "não informado"
    try:
        return datetime.fromisoformat(data_iso).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return data_iso


def bloco_licitacao(licitacao: Licitacao, prefixo: str = "") -> dict[str, Any]:
    tag = "🌍 *possível importado*" if licitacao.sinal_importacao else "🔎 candidato (verificar)"
    texto = (
        f"{prefixo}*{licitacao.orgao}* — {licitacao.municipio}/{licitacao.uf}\n"
        f"{tag} · _{', '.join(licitacao.segmentos)}_\n"
        f"*Objeto:* {licitacao.objeto}\n"
        f"*Valor estimado:* {formatar_valor(licitacao.valor_estimado)}   "
        f"*Modalidade:* {licitacao.modalidade}\n"
        f"*Prazo proposta:* {formatar_data(licitacao.encerramento_proposta)}\n"
        f"<{licitacao.link}|Abrir edital no PNCP>"
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": texto}}


def enviar_slack(token: str, canal: str, blocos: list[dict[str, Any]], texto_fallback: str) -> None:
    resp = requests.post(
        SLACK_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": canal, "blocks": blocos, "text": texto_fallback},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Slack API retornou erro: {payload}")


def enviar_em_lotes(token: str, canal: str, blocos: list[dict[str, Any]], texto_fallback: str, tamanho_lote: int = 40) -> None:
    if not blocos:
        return
    for i in range(0, len(blocos), tamanho_lote):
        enviar_slack(token, canal, blocos[i : i + tamanho_lote], texto_fallback)
        time.sleep(1)


def main() -> None:
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    canal_captacao = os.environ["SLACK_CHANNEL_CAPTACAO"]
    canal_followup = os.environ["SLACK_CHANNEL_FOLLOWUP"]

    hoje = date.today()
    estado = carregar_estado()
    licitacoes = buscar_licitacoes_abertas()

    novas = [l for l in licitacoes if l.numero_controle not in estado]
    novas.sort(key=lambda l: (not l.sinal_importacao, -(l.valor_estimado or 0)))

    if novas:
        blocos = [{"type": "header", "text": {"type": "plain_text", "text": f"📋 {len(novas)} nova(s) licitação(ões) — {hoje.strftime('%d/%m/%Y')}"}}]
        for licitacao in novas:
            blocos.append(bloco_licitacao(licitacao))
            blocos.append({"type": "divider"})
        enviar_em_lotes(slack_token, canal_captacao, blocos, texto_fallback=f"{len(novas)} novas licitações encontradas")
    else:
        enviar_slack(
            slack_token,
            canal_captacao,
            [{"type": "section", "text": {"type": "mrkdwn", "text": f"Nenhuma licitação nova encontrada esta semana ({hoje.strftime('%d/%m/%Y')})."}}],
            texto_fallback="Nenhuma licitação nova",
        )

    alertas_followup = []
    limite_alerta = hoje + timedelta(days=FOLLOWUP_ALERTA_DIAS)
    todas_por_id = {l.numero_controle: l for l in licitacoes}

    for numero_controle, registro in estado.items():
        encerramento_str = registro.get("encerramento_proposta")
        if not encerramento_str:
            continue
        try:
            encerramento = datetime.fromisoformat(encerramento_str).date()
        except ValueError:
            continue
        if hoje <= encerramento <= limite_alerta and not registro.get("alertado_followup"):
            licitacao_atual = todas_por_id.get(numero_controle)
            alertas_followup.append((numero_controle, licitacao_atual, registro))

    if alertas_followup:
        blocos = [{"type": "header", "text": {"type": "plain_text", "text": f"⏰ Prazos se aproximando ({hoje.strftime('%d/%m/%Y')})"}}]
        for numero_controle, licitacao_atual, registro in alertas_followup:
            if licitacao_atual is not None:
                blocos.append(bloco_licitacao(licitacao_atual))
            else:
                blocos.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*{registro.get('orgao', '(órgão não informado)')}*\n"
                                f"*Objeto:* {registro.get('objeto', '')}\n"
                                f"*Prazo proposta:* {formatar_data(registro.get('encerramento_proposta'))}\n"
                                f"<{registro.get('link', '')}|Abrir edital no PNCP>"
                            ),
                        },
                    }
                )
            blocos.append({"type": "divider"})
            estado[numero_controle]["alertado_followup"] = True
        enviar_em_lotes(slack_token, canal_followup, blocos, texto_fallback=f"{len(alertas_followup)} prazo(s) se aproximando")

    for licitacao in novas:
        estado[licitacao.numero_controle] = {
            "orgao": licitacao.orgao,
            "objeto": licitacao.objeto,
            "uf": licitacao.uf,
            "modalidade": licitacao.modalidade,
            "valor_estimado": licitacao.valor_estimado,
            "encerramento_proposta": licitacao.encerramento_proposta,
            "link": licitacao.link,
            "sinal_importacao": licitacao.sinal_importacao,
            "primeira_captura": hoje.isoformat(),
            "alertado_followup": False,
        }

    limite_limpeza = hoje - timedelta(days=30)
    for numero_controle in list(estado.keys()):
        encerramento_str = estado[numero_controle].get("encerramento_proposta")
        if not encerramento_str:
            continue
        try:
            encerramento = datetime.fromisoformat(encerramento_str).date()
        except ValueError:
            continue
        if encerramento < limite_limpeza:
            del estado[numero_controle]

    salvar_estado(estado)
    print(f"OK: {len(novas)} novas, {len(alertas_followup)} alertas de prazo, {len(estado)} no estado.")


if __name__ == "__main__":
    main()
