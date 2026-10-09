#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de coleta de dados (web scraping) com notificacao no Telegram e/ou Discord.

COMO USAR (resumo — o passo a passo completo esta no README.md):

    python3 bot.py --testar      # testa se a notificacao esta configurada certa
    python3 bot.py               # roda a coleta UMA vez
    python3 bot.py --loop        # roda a cada 15 minutos, sem parar

Toda a configuracao sensivel (token do bot, id do grupo) vem do arquivo .env.
NADA de senha ou token fica escrito dentro deste arquivo.
"""

from __future__ import annotations   # compatibilidade com Python 3.9

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==========================================================================
#  1. CONFIGURACAO DO SITE ALVO (cssdeals.com)
# ==========================================================================
#
#  IMPORTANTE — POR QUE NAO PRECISAMOS VARRER ABA POR ABA:
#
#  O site tem dezenas de abas (Shoes, Hoodie, Pants...) e cada uma tem
#  subabas de tamanho, com 20 produtos por pagina e as vezes 20+ paginas.
#  Varrer tudo isso daria centenas de requisicoes a cada rodada.
#
#  Nao e necessario. O site tem uma API interna que devolve os produtos
#  JA ORDENADOS DO MAIS NOVO PARA O MAIS ANTIGO, misturando todas as
#  categorias e tamanhos. Como voce so quer LANCAMENTOS NOVOS, basta ler
#  a primeira pagina dessa lista: o que apareceu de novo desde a ultima
#  vez esta sempre no topo.
#
#  Resultado: 1 requisicao a cada 15 minutos, em vez de centenas — e
#  mesmo assim nenhum lancamento escapa, de nenhuma categoria.
# ==========================================================================

SITE_BASE = "https://cssdeals.com"

# Endereco da lista de produtos (a API interna do proprio site)
API_PRODUTOS = SITE_BASE + "/api/product"

# Quantos produtos ler por rodada (do mais novo para o mais antigo).
# 50 da uma margem folgada: mesmo que o site cadastre varios produtos
# em 15 minutos, nenhum passa despercebido.
TAMANHO_PAGINA = 100          # maximo aceito pela API (acima disso ela devolve 20!)

# --- Varredura profunda ---
#  A API ordena por ID, que e a ordem de CRIACAO do produto — nao a de
#  publicacao. O cssdeals as vezes torna visivel um produto criado ha
#  dias: ele carrega o ID antigo e nasce no MEIO da lista, nunca no topo.
#
#  Ler so o topo deixaria esses produtos invisiveis para sempre, porque
#  eles nunca sobem. Por isso, de tempos em tempos o bot varre uma janela
#  bem mais funda procurando qualquer ID que ainda nao conheca,
#  independentemente da posicao.
PAGINAS_PROFUNDAS = 10        # 10 x 100 = 1000 produtos (~3 dias)

# MEDIDO EM 28/08/2026: das 5 publicacoes observadas em 3 horas,
# NENHUMA caiu dentro dos 100 do topo. Elas entraram nas posicoes
# 317, 324, 485, 573 e 783.
#
# Ou seja: a leitura rasa nao pega quase nada neste site — quem faz o
# trabalho e a varredura profunda. Por isso ela roda a cada 5 minutos,
# e nao a cada 20: o intervalo dela E o atraso real dos seus avisos.
#
# 5 minutos = 10 requisicoes a cada 5 min (~120 por hora). E um ritmo
# defensavel para monitoramento; nao baixe muito mais que isso sem
# necessidade, por respeito ao servidor do site.
# Valor padrao; o real e lido do .env em carregar_config().
# Cada varredura = 10 requisicoes ao site. A cada 2 min sao ~300 por
# hora — ritmo defensavel. Descer para 1 min dobraria isso, e o risco
# nao e etico e sim pratico: site que bloqueia seu IP te deixa com
# ZERO alertas, o que e muito pior que 2 minutos de atraso.
# Segundos entre varreduras profundas, fora do horario de pico.
#
# Cada varredura sao 10 requisicoes ao site. A 45s isso da ~800 por
# hora; no pico de 15s, ~2400. E um ritmo alto — se o site comecar a
# recusar ou dar timeout, afrouxe este numero.
VARREDURA_PADRAO_SEG = 5

# --- Janela de pico ---
# No horario em que o site despeja muitos itens de uma vez, o bot varre
# com muito mais frequencia. Fora dela, volta ao ritmo normal.
#
# ATENCAO AO FUSO: o servidor roda em UTC. FUSO_HORAS=-3 faz o bot
# raciocinar no horario de Brasilia, que e como voce pensa os horarios.
PICO_INICIO_PADRAO = "22:00"
PICO_FIM_PADRAO    = "07:30"
PICO_SEGUNDOS_PADRAO = 5
FUSO_PADRAO = -3


def _hora_local(fuso: int):
    """Hora atual no fuso configurado (o servidor roda em UTC)."""
    from datetime import timedelta, timezone as tz
    return datetime.now(tz(timedelta(hours=fuso)))


def _para_minutos(texto: str, padrao: str) -> int:
    """Converte '22:00' em minutos desde a meia-noite."""
    try:
        h, m = texto.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        h, m = padrao.split(":")
        return int(h) * 60 + int(m)


def em_horario_de_pico(config: dict) -> bool:
    """Diz se agora estamos na janela de pico (ex: 22:00 as 07:30)."""
    if not config.get("pico_segundos"):
        return False
    agora = _hora_local(config["fuso"])
    minutos = agora.hour * 60 + agora.minute
    ini, fim = config["pico_inicio"], config["pico_fim"]
    # janela que atravessa a meia-noite
    return (minutos >= ini or minutos < fim) if ini > fim else (ini <= minutos < fim)
DELAY_ENTRE_PAGINAS = 1.0

# Quantas paginas buscar ao mesmo tempo na varredura profunda.
# NAO aumenta o numero de requisicoes — apenas evita que uma espere a
# outra. A varredura cai de ~25s para ~3s, e esse tempo saia do seu
# atraso. 4 simultaneas e um meio-termo: rapido sem parecer ataque.
PAGINAS_SIMULTANEAS = 4

# De quanto em quanto tempo fazer a varredura PROFUNDA (10 paginas). Antes ela
# rodava a CADA ciclo (~1,4 requisicao/s o dia todo, 10 conexoes novas de uma
# vez) — o aumento de fluxo de 07/10 coincidiu com os erros de conexao diarios
# (HTTPSConnectionPool: Max retries exceeded). A leitura rapida (2 paginas)
# continua a cada ciclo e pega os lancamentos normais; a profunda so serve para
# o produto que o site publica no MEIO da lista, e 20s de espera para ele e
# pouco. Pode mudar com VARREDURA_PROFUNDA_SEG no Railway (minimo 5).
VARREDURA_PROFUNDA_PADRAO_SEG = 20

# Paginas da leitura RAPIDA (a de cada ciclo).
#
# MEDIDO EM 29/08/2026, manha: das 11 publicacoes, 8 cairam nos 100
# primeiros e 3 nas posicoes 103, 116 e 132 — escapando por pouco de
# uma leitura de 1 pagina. Com 2 paginas (200 produtos), as 11 seriam
# pegas no ciclo rapido.
#
# Custo: 1 requisicao a mais por ciclo (~60 por hora).
PAGINAS_RAPIDAS = 2

# Detalhe de um produto: e o unico lugar que traz TODAS as fotos do
# anuncio feito no CSSDeals. A listagem devolve so uma imagem, que as
# vezes vem do site de origem (1688/Taobao/Weidian) em vez do CSSDeals.
API_DETALHE = SITE_BASE + "/api/product/{id}"

# Qual foto do anuncio usar: 0 = primeira, 1 = segunda, e assim por diante.
# Valor padrao; o real e lido do .env em carregar_config().
FOTO_PADRAO = 0

# O servidor de imagens do CSSDeals redimensiona pela propria URL.
# A foto original tem ~3 MB; assim cai para ~80 KB. Isso importa porque
# o Telegram BAIXA a imagem a cada envio — foto grande atrasa a entrega.
REDIMENSIONA_FOTO = "?x-oss-process=image/resize,w_800/quality,Q_80"

# Pagina do produto no CSSDeals
URL_PRODUTO = SITE_BASE + "/product-detail.html?itemid={id}"

# Link que joga o produto direto no carrinho do CSSBuy.
#
# Descoberto no main.js do site: o botao "Buy Now" faz
#     POST /api/cart {productId, quantity}
# e recebe de volta um redirectUrl com este formato. Como ele so depende
# do id do produto no CSSDeals — que ja temos —, da para montar aqui
# mesmo, sem requisicao nenhuma.
URL_COMPRA = "https://www.cssbuy.com/waiting?type=cssdeals&productId={id}&quantity=1"

# Trecho extra colado no fim do link de compra — serve para o seu codigo
# de indicacao do CSSBuy, que gera comissao quando alguem compra.
#
# Configure em CSSBUY_EXTRA no Railway, com o parametro exatamente como
# aparece no SEU link de indicacao. Exemplos:
#     &promotecode=SEUCODIGO
#     &invitecode=SEUCODIGO
#     &ref=SEUCODIGO
#
# Deixe vazio para nao acrescentar nada.
_extra_compra = ""

# Nome de cada plataforma de origem (vem no campo salePlatform)
PLATAFORMAS = {1: "Taobao", 2: "Weidian", 3: "1688"}

# --- Canais por grupo de categoria ---
#
# Cada grupo vira um canal do Discord. Voce so precisa criar o webhook e
# colar na variavel correspondente no Railway (ex: CANAL_CALCADOS).
# Nao precisa digitar numero de categoria — o mapa ja esta pronto aqui.
#
# Produto de categoria que nao esta em nenhum grupo cai em OUTROS.
GRUPOS = {
    "CALCADOS":    ["11"],
    "ROUPAS":      ["14", "15", "32", "12", "35", "33", "34", "31", "40", "18"],
    "ACESSORIOS":  ["26", "27", "30", "39", "44", "45", "16", "41"],
    "ELETRONICOS": ["20", "21", "22", "38", "23", "24", "19"],
    "OUTROS":      ["36", "37", "43"],
}

# Nome bonito de cada grupo, para os logs
NOME_GRUPO = {
    "CALCADOS": "Calcados", "ROUPAS": "Roupas", "ACESSORIOS": "Acessorios",
    "ELETRONICOS": "Eletronicos", "OUTROS": "Outros",
}


def grupo_do_produto(categoria_id: str) -> str:
    """Descobre em qual grupo/canal o produto se encaixa."""
    for grupo, categorias in GRUPOS.items():
        if str(categoria_id) in categorias:
            return grupo
    return "OUTROS"


# Categorias do site — usado so para mostrar o nome na mensagem.
# (levantado de https://cssdeals.com/api/category/tree)
CATEGORIAS = {
    "11": "Shoes", "12": "Coat", "14": "T-shirts", "15": "Pants",
    "16": "Accessories", "18": "Fashion", "19": "Electronics",
    "20": "Watches", "21": "Cell phone", "22": "Earphone",
    "23": "Computer accessories", "24": "Audio & Video",
    "26": "Hat&Bags", "27": "Belt&Glasses", "30": "Gloves& Scarf",
    "31": "Underwear & Sleepwear", "32": "Hoodie", "33": "down jacket",
    "34": "suit", "35": "long sleeve", "36": "sports goods",
    "37": "toy", "38": "phone case", "39": "accessories",
    "40": "socks", "41": "Accessories", "43": "sports goods",
    "44": "perfume", "45": "suitcase",
}


# ==========================================================================
#  2. CONFIGURACOES GERAIS
# ==========================================================================
# (algumas podem ser ajustadas por variavel de ambiente, sem mexer no codigo)

# Arquivo SQLite onde tudo fica salvo. No Railway o disco comum e apagado a
# cada reinicio; com um Volume montado em /data, use CAMINHO_BANCO=/data/dados.db
# e o bot passa a lembrar o que ja avisou mesmo depois de reiniciar.
BANCO_DADOS = os.getenv("CAMINHO_BANCO", "").strip() or "dados.db"
ARQUIVO_LOG = "bot.log"           # historico do que o bot fez
# Segundos entre cada rodada no modo --loop (servidor sempre ligado).
# 60s da o menor atraso possivel sem pesar no site: e 1 consulta por
# minuto, enquanto o site publica ~2 produtos a cada 15 minutos.
# O valor real e lido do .env em carregar_config() — este e so o padrao.
INTERVALO_PADRAO = 60
DELAY_ENTRE_REQUISICOES = 1.5     # segundos de pausa entre paginas do site
DELAY_ENTRE_MENSAGENS = 1.2       # segundos entre mensagens (limite do Telegram)
TIMEOUT = 30                      # segundos ate desistir de uma requisicao
TIMEOUT_CATALOGO = 10             # idem, so para as paginas do catalogo (normalmente <1s)
TELEGRAM_PAUSA_SEG = 60           # apos falhas seguidas, deixa o Telegram quieto por este tempo
MAX_TENTATIVAS = 3                # quantas vezes tentar de novo se der erro
MAX_NOTIFICACOES_POR_RODADA = 20  # trava de seguranca contra spam

# --- Traducao dos titulos ---
# Os titulos vem do site em chines e ingles. O bot traduz para portugues
# usando o MyMemory, que e gratuito e NAO precisa de cadastro nem chave.
API_TRADUCAO = "https://api.mymemory.translated.net/get"

# --- Conversao de Yuan (CN¥) para Real (R$) ---
# Duas fontes gratuitas, sem cadastro. A segunda so e usada se a
# primeira falhar. A cotacao e buscada uma vez por hora, nao a cada
# produto.
FONTES_COTACAO = [
    ("AwesomeAPI", "https://economia.awesomeapi.com.br/last/CNY-BRL"),
    ("ExchangeRate", "https://open.er-api.com/v6/latest/CNY"),
]
VALIDADE_COTACAO = 3600   # segundos (1 hora)

# Mostrar tambem o valor em reais? Desligado: os precos aparecem so em
# Yuan, como o site publica. Para ligar, use MOSTRAR_REAL=sim no .env.
_mostrar_real = False

# Indice da foto usada nos avisos (definido em carregar_config)
_foto_escolhida = FOTO_PADRAO
IDIOMA_DESTINO = "pt-BR"
DELAY_ENTRE_TRADUCOES = 0.5   # segundos entre traducoes (educacao com o servico)
LIMITE_TEXTO_TRADUCAO = 480   # o MyMemory aceita ate ~500 caracteres por vez

# User-Agent honesto: diz o que o programa e, em vez de fingir ser um navegador.
# O dono do site consegue ver quem esta acessando e para que.
#
# Historico: ate 06/10/2026 era "BotColetaPessoal/1.0". Em 07/10 o Cloudflare
# do cssdeals passou a responder 403 "Just a moment..." a esse nome (user-agents
# de navegador e curl passavam). Por decisao do usuario, o nome foi trocado por
# outro que continua dizendo claramente que e um monitor automatico. NAO
# imita navegador. Se este nome tambem for barrado, a saida e a autorizacao
# do CSSBuy/CSSDeals (API oficial), nao disfarce.
USER_AGENT = "ColetaPessoal/1.0 (monitor de lancamentos para uso pessoal)"


# ==========================================================================
#  3. LOG (mostra o progresso na tela e salva no arquivo bot.log)
# ==========================================================================

# Mensagens de erro de rede trazem o endereco chamado — e nele vai o token
# do Telegram ou o segredo do webhook do Discord. Estes padroes apagam
# isso de TODA linha de log, inclusive tracebacks.
_SEGREDOS_NO_LOG = [
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), "<TOKEN-OCULTO>"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+"), r"\1<OCULTO>"),
]


class FormatoSemSegredos(logging.Formatter):
    def format(self, record):
        texto = super().format(record)
        for padrao, troca in _SEGREDOS_NO_LOG:
            texto = padrao.sub(troca, texto)
        return texto


def configurar_log() -> logging.Logger:
    logger = logging.getLogger("bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formato = FormatoSemSegredos(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%d/%m/%Y %H:%M:%S"
    )

    # mostra na tela
    tela = logging.StreamHandler(sys.stdout)
    tela.setFormatter(formato)
    logger.addHandler(tela)

    # salva no arquivo
    arquivo = logging.FileHandler(ARQUIVO_LOG, encoding="utf-8")
    arquivo.setFormatter(formato)
    logger.addHandler(arquivo)

    return logger


log = configurar_log()


# ==========================================================================
#  4. BANCO DE DADOS (SQLite) — guarda os itens e evita duplicatas
# ==========================================================================

def abrir_banco() -> sqlite3.Connection:
    """Abre (ou cria, na primeira vez) o arquivo dados.db."""
    pasta = os.path.dirname(BANCO_DADOS)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    conexao = sqlite3.connect(BANCO_DADOS)
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS itens (
            id            TEXT PRIMARY KEY,   -- id do produto no proprio site
            titulo        TEXT NOT NULL,      -- titulo original (chines/ingles)
            titulo_pt     TEXT,               -- titulo traduzido para portugues
            tamanho       TEXT,               -- tamanho do produto (P/M/G, 42...)
            imagem        TEXT,
            link          TEXT,
            preco         TEXT,
            categoria     TEXT,
            plataforma    TEXT,
            origem        TEXT,
            visto_em      TEXT NOT NULL,      -- quando o bot viu pela 1a vez
            notificado    INTEGER DEFAULT 0,  -- 1 = TODOS os canais configurados receberam
            grupo         TEXT,               -- calcados/roupas/... (para o canal certo do Discord)
            tg_ok         INTEGER DEFAULT 0,  -- 1 = ja chegou no Telegram
            discord_ok    INTEGER DEFAULT 0,  -- 1 = ja chegou no canal geral do Discord
            canal_ok      INTEGER DEFAULT 0   -- 1 = ja chegou no canal da categoria (Discord)
        )
        """
    )
    conexao.execute(
        "CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT)"
    )
    # Bancos criados antes desta versao nao tem estas colunas.
    # Adiciona sem apagar nada do que ja estava salvo.
    colunas = [c[1] for c in conexao.execute("PRAGMA table_info(itens)")]
    for nome, tipo in (("tamanho", "TEXT"), ("grupo", "TEXT"),
                       ("tg_ok", "INTEGER DEFAULT 0"),
                       ("discord_ok", "INTEGER DEFAULT 0"),
                       ("canal_ok", "INTEGER DEFAULT 0")):
        if nome not in colunas:
            conexao.execute(f"ALTER TABLE itens ADD COLUMN {nome} {tipo}")
            log.info("Banco atualizado: coluna '%s' adicionada.", nome)

    conexao.commit()
    return conexao


def banco_vazio(conexao: sqlite3.Connection) -> bool:
    """Diz se e a primeirissima vez que o bot roda (banco ainda sem nada)."""
    return conexao.execute("SELECT COUNT(*) FROM itens").fetchone()[0] == 0


def item_ja_existe(conexao: sqlite3.Connection, item_id: str) -> bool:
    cursor = conexao.execute("SELECT 1 FROM itens WHERE id = ?", (item_id,))
    return cursor.fetchone() is not None


def salvar_item(conexao: sqlite3.Connection, item: dict,
                ja_notificado: bool = False, visto_em: Optional[str] = None) -> None:
    """
    Salva UM item e grava no disco na hora (commit).

    Isso e o 'salvamento incremental': se o script travar no meio, tudo que
    ja foi coletado ate ali continua salvo.

    `ja_notificado=True` e usado so na primeira rodada, para registrar o que
    ja existia no site sem te encher de mensagens.
    """
    # Item que ja nasce sem estoque (esgotou na fila) entra direto como
    # concluido nos 3 canais — nao ha o que reenviar.
    tudo_ok = 1 if ja_notificado else 0
    conexao.execute(
        """
        INSERT OR IGNORE INTO itens
            (id, titulo, titulo_pt, tamanho, imagem, link, preco, categoria,
             plataforma, origem, visto_em, notificado, grupo,
             tg_ok, discord_ok, canal_ok)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["id"], item["titulo"], item.get("titulo_pt", ""),
            item.get("tamanho", ""),
            item["imagem"], item["link"],
            item["preco"], item["categoria"], item["plataforma"], item["origem"],
            visto_em or datetime.now().isoformat(timespec="seconds"),
            1 if ja_notificado else 0,
            item.get("grupo", "OUTROS"),
            tudo_ok, tudo_ok, tudo_ok,
        ),
    )
    conexao.commit()


def guardar_piso(conexao: sqlite3.Connection, ids: list) -> int:
    """
    Grava o MENOR id da janela de referencia (a da primeira rodada).

    Ele separa lancamento de "produto antigo que so apareceu": o catalogo
    encolhe quando itens vendem, e cada item removido la em cima empurra um
    item antigo, de FORA da janela de 1000, para dentro dela. O bot nunca
    tinha visto esse item e o anunciava como novo — mesmo ele tendo semanas.
    Na pratica, em 04/10/2026 a janela cobria 32 dias (antes ~3).

    Id MENOR que o piso = mais velho que tudo o que o bot viu na partida =
    entrou por baixo. Nao e lancamento.
    """
    piso = min(int(i) for i in ids)
    conexao.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES ('piso_id', ?)",
                    (str(piso),))
    conexao.commit()
    return piso


def obter_piso(conexao: sqlite3.Connection) -> int:
    linha = conexao.execute("SELECT valor FROM meta WHERE chave = 'piso_id'").fetchone()
    if linha:
        return int(linha[0])
    # Banco antigo, sem piso gravado: usa o menor id que ele ja conhece.
    linha = conexao.execute("SELECT MIN(CAST(id AS INTEGER)) FROM itens").fetchone()
    if linha and linha[0]:
        return int(guardar_piso(conexao, [linha[0]]))
    return 0


# Quanto tempo um aviso pendente continua valendo. Passou disso, o produto
# (que tem 1 unidade em 98% dos casos) ja foi vendido ou removido — avisar
# so manda o cliente para uma pagina vazia. Sem este limite, um canal com
# falha persistente fazia o item ser tentado para sempre, dias depois.
VALIDADE_PENDENTE_MIN = int(os.getenv("VALIDADE_PENDENTE_MIN", "10") or 10)


def expirar_pendentes(conexao: sqlite3.Connection) -> int:
    """Desiste dos avisos pendentes velhos demais. Devolve quantos."""
    limite = (datetime.now() - timedelta(minutes=VALIDADE_PENDENTE_MIN)).isoformat(timespec="seconds")
    cursor = conexao.execute(
        "UPDATE itens SET notificado = 1 WHERE notificado = 0 AND visto_em < ?", (limite,)
    )
    conexao.commit()
    return cursor.rowcount


def buscar_pendentes(conexao: sqlite3.Connection) -> list:
    """
    Devolve os itens que ainda faltam em ALGUM canal.

    Por que isso existe: se o Telegram estiver fora do ar (ou o token
    errado) mas o Discord funcionar, o item PRECISA continuar pendente
    para o Telegram — nao pode ser dado como concluido so porque um dos
    canais recebeu. Por isso cada canal tem sua propria coluna de status
    (tg_ok, discord_ok, canal_ok), e so viram 'notificado=1' quando TODOS
    os canais configurados no momento tiverem recebido.
    """
    cursor = conexao.execute(
        """
        SELECT id, titulo, titulo_pt, tamanho, imagem, link, preco,
               categoria, plataforma, origem, grupo, tg_ok, discord_ok, canal_ok
        FROM itens WHERE notificado = 0 ORDER BY visto_em, rowid
        """
    )
    return [
        {"id": l[0], "titulo": l[1], "titulo_pt": l[2], "tamanho": l[3],
         "imagem": l[4], "link": l[5], "preco": l[6], "categoria": l[7],
         "plataforma": l[8], "origem": l[9], "grupo": l[10] or "OUTROS",
         "compra": URL_COMPRA.format(id=l[0]) + _extra_compra,
         "_tg_ok": bool(l[11]), "_discord_ok": bool(l[12]), "_canal_ok": bool(l[13])}
        for l in cursor.fetchall()
    ]


def marcar_notificado(conexao: sqlite3.Connection, item_id: str) -> None:
    """Marca o item como concluido em TODOS os canais (ex: ja esgotou)."""
    conexao.execute(
        "UPDATE itens SET notificado = 1, tg_ok = 1, discord_ok = 1, canal_ok = 1 "
        "WHERE id = ?", (item_id,)
    )
    conexao.commit()


def enviar_por_canais(item: dict, config: dict) -> dict:
    """
    Manda o item so nos canais que AINDA nao o receberam.

    Um item novo (recem extraido, nunca salvo) nao tem _tg_ok/_discord_ok/
    _canal_ok — trata como False em todos, ou seja, tenta todos os canais
    configurados. Um item retomado (vindo de buscar_pendentes) so tenta de
    novo o que faltou da vez anterior — nao reenvia ao que ja funcionou.

    Devolve o status ATUALIZADO de cada canal (True = confirmado entregue
    agora ou em rodada anterior; False = ainda falta).
    """
    tg_ok = item.get("_tg_ok", False)
    discord_ok = item.get("_discord_ok", False)
    canal_ok = item.get("_canal_ok", False)

    tem_telegram = bool(config["telegram_token"] and config["telegram_chat_id"])
    tem_discord_geral = bool(config["discord_webhook"])
    webhook_canal = config.get("canais", {}).get(item.get("grupo", ""))

    if tem_telegram and not tg_ok:
        tg_ok = enviar_telegram(item, config["telegram_token"], config["telegram_chat_id"])
        if not tg_ok:
            log.warning("Telegram NAO recebeu (vai tentar de novo na proxima rodada): %s",
                       item["titulo"][:50])

    if tem_discord_geral and not discord_ok:
        discord_ok = enviar_discord(item, config["discord_webhook"])
        if not discord_ok:
            log.warning("Discord (canal geral) NAO recebeu (vai tentar de novo): %s",
                       item["titulo"][:50])

    if webhook_canal and not canal_ok:
        canal_ok = enviar_discord(item, webhook_canal)
        if not canal_ok:
            log.warning("Discord/%s NAO recebeu (vai tentar de novo): %s",
                       NOME_GRUPO.get(item.get("grupo", ""), item.get("grupo")),
                       item["titulo"][:50])

    # Canais que NAO estao configurados contam como "ok" — nao ha o que
    # entregar ali, entao nao devem travar o item como pendente para sempre.
    return {
        "tg_ok": tg_ok or not tem_telegram,
        "discord_ok": discord_ok or not tem_discord_geral,
        "canal_ok": canal_ok or not webhook_canal,
    }


def salvar_status_canais(conexao: sqlite3.Connection, item_id: str, status: dict) -> bool:
    """
    Grava o status por canal. Devolve True se o item ficou COMPLETO
    (todos os canais aplicaveis receberam) — nesse caso vira notificado=1
    e nao aparece mais em buscar_pendentes.
    """
    completo = status["tg_ok"] and status["discord_ok"] and status["canal_ok"]
    conexao.execute(
        "UPDATE itens SET tg_ok = ?, discord_ok = ?, canal_ok = ?, notificado = ? "
        "WHERE id = ?",
        (int(status["tg_ok"]), int(status["discord_ok"]), int(status["canal_ok"]),
         int(completo), item_id),
    )
    conexao.commit()
    return completo


# ==========================================================================
#  5. COLETA — baixar a pagina e extrair os dados
# ==========================================================================

# A sessao e reaproveitada entre as rodadas (conexao HTTPS mantida aberta): antes,
# cada rodada abria ate 10 conexoes novas com handshake TLS completo.
_sessao_global = {"obj": None}


def obter_sessao() -> requests.Session:
    if _sessao_global["obj"] is None:
        _sessao_global["obj"] = criar_sessao()
    return _sessao_global["obj"]


def descartar_sessao() -> None:
    """Joga fora as conexoes guardadas (depois de uma falha, comeca limpo)."""
    sessao, _sessao_global["obj"] = _sessao_global["obj"], None
    if sessao is not None:
        try:
            sessao.close()
        except Exception:
            pass


def criar_sessao() -> requests.Session:
    """
    Prepara o 'navegador' do bot com retry automatico.

    Se der timeout ou conexao recusada, ele tenta de novo ate 3 vezes,
    esperando um pouco mais a cada tentativa (1s, 2s, 4s).
    """
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": USER_AGENT})

    # Poucas tentativas aqui dentro: a rodada seguinte ja e a nova tentativa, e
    # insistir 4x em 10 paginas ao mesmo tempo so aumenta a carga num site que
    # ja esta nao respondendo.
    politica_retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adaptador = HTTPAdapter(max_retries=politica_retry, pool_connections=2,
                            pool_maxsize=PAGINAS_SIMULTANEAS + 2)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    return sessao


# Guarda a leitura do robots.txt por um tempo, para nao buscar o arquivo
# a cada rodada (no pico sao 4 rodadas por minuto).
_robots_cache = {"leitor": None, "quando": 0.0}
ROBOTS_VALIDADE = 3600   # segundos


def robots_permite(url: str) -> bool:
    """
    Le o robots.txt do site e confere se o bot tem permissao de acessar.

    IMPORTANTE: o arquivo e buscado com a identificacao do PROPRIO bot.
    O leitor padrao do Python usa "Python-urllib", que o Cloudflare do
    cssdeals passou a bloquear (erro 1010) em 15/09/2026. O leitor
    recebia 403 e, por regra antiga dele, tratava isso como "tudo
    proibido" — o bot parou de enviar por horas sem o site ter proibido
    nada.

    Regras (RFC 9309):
      - 200          -> obedece o que o arquivo diz
      - 4xx          -> arquivo indisponivel = sem restricoes (com aviso)
      - 5xx / rede   -> nao da para saber; segue com cautela (com aviso)
    """
    agora = time.time()
    base = "{}://{}".format(urlparse(url).scheme, urlparse(url).netloc)

    if _robots_cache["leitor"] is None or agora - _robots_cache["quando"] > ROBOTS_VALIDADE:
        leitor = urllib.robotparser.RobotFileParser()
        try:
            resposta = requests.get(urljoin(base, "/robots.txt"),
                                    headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            if resposta.status_code == 200:
                leitor.parse(resposta.text.splitlines())
            elif 400 <= resposta.status_code < 500:
                log.warning("robots.txt indisponivel (HTTP %s). Sem restricoes declaradas.",
                            resposta.status_code)
                leitor.parse([])
            else:
                log.warning("robots.txt respondeu HTTP %s. Seguindo com cautela.",
                            resposta.status_code)
                leitor.parse([])
        except Exception as erro:
            log.warning("Nao consegui ler o robots.txt (%s). Seguindo com cautela.",
                        str(erro)[:80])
            leitor.parse([])
        _robots_cache["leitor"] = leitor
        _robots_cache["quando"] = agora

    permitido = _robots_cache["leitor"].can_fetch(USER_AGENT, url)
    if not permitido:
        log.error("robots.txt do site PROIBE o acesso a %s — coleta cancelada.", url)
    return permitido


def baixar_pagina(sessao: requests.Session, url: str) -> Optional[str]:
    """Baixa o HTML da pagina. Devolve None se falhar em todas as tentativas."""
    try:
        resposta = sessao.get(url, timeout=TIMEOUT)
        resposta.raise_for_status()
        return resposta.text
    except requests.exceptions.Timeout:
        log.error("Timeout: o site demorou mais de %ss para responder.", TIMEOUT)
    except requests.exceptions.ConnectionError:
        log.error("Conexao recusada ou sem internet.")
    except requests.exceptions.HTTPError as erro:
        log.error("O site respondeu com erro: %s", erro)
    except Exception as erro:
        log.error("Erro inesperado ao baixar a pagina: %s", erro)
    return None


# Devolvido por _buscar_pagina quando o Cloudflare responde com o desafio
# "Just a moment..." (HTTP 403 + cabecalho cf-mitigated: challenge). Isso NAO
# e erro de codigo nem de rede: e o site decidindo nao atender este cliente.
DESAFIO = object()

# Estado do bloqueio: quantas rodadas seguidas foram barradas e ate quando o
# bot deve ficar quieto. Quando o site barra, insistir (10 requisicoes a cada
# 5s) so piora — o bot recua, espera cada vez mais e sonda com UMA requisicao.
_bloqueio = {"seguidos": 0, "ate": 0.0}
ESPERAS_BLOQUEIO = [60, 120, 300, 600]   # segundos; trava em 10 min


def registrar_bloqueio() -> None:
    n = _bloqueio["seguidos"]
    _bloqueio["seguidos"] = n + 1
    espera = ESPERAS_BLOQUEIO[min(n, len(ESPERAS_BLOQUEIO) - 1)]
    _bloqueio["ate"] = time.time() + espera
    if n == 0:
        log.error("=" * 60)
        log.error("O CLOUDFLARE DO CSSDEALS ESTA BARRANDO O BOT.")
        log.error("   O site responde HTTP 403 'Just a moment...' (ou 429) a este bot.")
        log.error("   Enquanto durar, o bot NAO le o catalogo e NAO avisa nada.")
        log.error("   Nao e erro de codigo: e o site recusando o cliente.")
        log.error("   O bot respeita o bloqueio: nao insiste, espera e sonda 1x.")
        log.error("=" * 60)
    elif n % 10 == 0:
        log.warning("Bloqueio do Cloudflare continua (%s tentativas). Proxima em %ss.", n + 1, espera)


def limpar_bloqueio() -> None:
    if _bloqueio["seguidos"]:
        log.info("O site voltou a responder normalmente (bloqueio terminou).")
    _bloqueio["seguidos"] = 0
    _bloqueio["ate"] = 0.0


# Falhas de conexao: o requests esconde o motivo real atras de "Max retries
# exceeded" (e o log cortava a mensagem em 70 caracteres). Aqui guardamos a
# CAUSA de cada pagina que falhou para o log mostrar um resumo claro.
_causas_falha = []
_falha_rede = {"seguidas": 0, "ate": 0.0}
ESPERAS_FALHA_REDE = [5, 10, 20, 40, 60]   # segundos; trava em 1 min


def _causa_curta(erro) -> str:
    atual = erro
    for _ in range(8):
        proximo = None
        if getattr(atual, "reason", None) is not None:           # urllib3 MaxRetryError
            proximo = atual.reason
        elif atual.args and isinstance(atual.args[0], BaseException):
            proximo = atual.args[0]                              # requests embrulha o urllib3
        elif atual.__cause__ is not None:
            proximo = atual.__cause__
        if proximo is None or proximo is atual:
            break
        atual = proximo
    texto = re.sub(r"0x[0-9a-fA-F]+", "", str(atual))
    return "%s: %s" % (type(atual).__name__, re.sub(r"\s+", " ", texto)[:110])


def registrar_falha_rede(total_paginas: int) -> None:
    n = _falha_rede["seguidas"]
    _falha_rede["seguidas"] = n + 1
    espera = ESPERAS_FALHA_REDE[min(n, len(ESPERAS_FALHA_REDE) - 1)]
    _falha_rede["ate"] = time.time() + espera
    contagem = {}
    for causa in _causas_falha:
        contagem[causa] = contagem.get(causa, 0) + 1
    resumo = "; ".join("%sx %s" % (q, c) for c, q in sorted(contagem.items(), key=lambda x: -x[1])[:3])
    log.error("SITE SEM RESPOSTA (%s falha(s) seguida(s)) — causa: %s. "
              "Nova tentativa em %ss.", n + 1, resumo or "desconhecida", espera)
    descartar_sessao()      # a proxima tentativa abre conexoes novas


def limpar_falha_rede() -> None:
    if _falha_rede["seguidas"]:
        log.info("A conexao com o site voltou (depois de %s falha(s) seguida(s)).",
                 _falha_rede["seguidas"])
    _falha_rede["seguidas"] = 0
    _falha_rede["ate"] = 0.0


def _buscar_pagina(sessao: requests.Session, categoria: str, numero: int):
    """Busca UMA pagina. Devolve (numero, registros) ou (numero, None)."""
    parametros = {
        "fields": 1, "categoryId": categoria, "page": numero,
        "pageSize": TAMANHO_PAGINA, "priceMin": "0.00", "priceMax": "99999.00",
    }
    try:
        resposta = sessao.get(API_PRODUTOS, params=parametros, timeout=TIMEOUT_CATALOGO)
        if resposta.status_code == 429:
            return numero, DESAFIO      # excesso de requisicoes: recua, nao insiste
        if resposta.status_code == 403 and (
            resposta.headers.get("cf-mitigated") == "challenge"
            or "Just a moment" in resposta.text[:800]
        ):
            return numero, DESAFIO
        resposta.raise_for_status()
        corpo = resposta.json()
    except Exception as erro:
        _causas_falha.append(_causa_curta(erro))
        return numero, None

    if corpo.get("code") != 0:
        log.error("A API recusou a pagina %s: %s", numero, corpo.get("msg"))
        return numero, None

    dados = corpo.get("data") or {}
    if numero == 1 and dados.get("total") is not None:
        log.info("Catalogo do site tem %s produtos no total.", dados["total"])
    return numero, (dados.get("records") or [])


def buscar_lancamentos(sessao: requests.Session, categoria: str = "",
                       paginas: int = 1) -> Optional[list]:
    """
    Le os produtos do site, do mais recente para o mais antigo.

    `paginas=1` e a leitura rapida. Valores maiores fazem a varredura
    profunda, que busca as paginas EM PARALELO para nao somar espera ao
    seu atraso — sao as mesmas requisicoes, so que sem fila.
    """
    # Em recuo (site barrando ou sem responder): nenhuma requisicao ate o prazo.
    if time.time() < _bloqueio["ate"] or time.time() < _falha_rede["ate"]:
        return None
    _causas_falha.clear()

    # Saindo de um bloqueio: sonda com UMA pagina antes de soltar as 10.
    if _bloqueio["seguidos"] and paginas > 1:
        _, amostra = _buscar_pagina(sessao, categoria, 1)
        if amostra is DESAFIO:
            registrar_bloqueio()
            return None

    if paginas <= 1:
        _, registros = _buscar_pagina(sessao, categoria, 1)
        if registros is DESAFIO:
            registrar_bloqueio()
            return None
        if registros is not None:
            limpar_bloqueio()
            limpar_falha_rede()
        else:
            registrar_falha_rede(1)
        return registros

    from concurrent.futures import ThreadPoolExecutor

    resultados = {}
    restantes = range(1, paginas + 1)
    if paginas > 2:
        # Varredura profunda: a pagina 1 vai SOZINHA primeiro, como sonda. Se o
        # site nao responde, nao adianta disparar mais 9 pedidos (cada um
        # esperaria o tempo limite inteiro) — e e justamente nessa hora que o
        # site menos precisa de rajada.
        _, primeira = _buscar_pagina(sessao, categoria, 1)
        resultados[1] = primeira
        restantes = range(2, paginas + 1) if isinstance(primeira, list) else range(0)
    with ThreadPoolExecutor(max_workers=PAGINAS_SIMULTANEAS) as executor:
        tarefas = [executor.submit(_buscar_pagina, sessao, categoria, n)
                   for n in restantes]
        for tarefa in tarefas:
            numero, registros = tarefa.result()
            resultados[numero] = registros

    if any(r is DESAFIO for r in resultados.values()):
        registrar_bloqueio()
        return None
    limpar_bloqueio()
    if resultados.get(1) is None:
        registrar_falha_rede(paginas)         # nem a primeira pagina veio
        return None
    limpar_falha_rede()
    falharam = sorted(n for n, r in resultados.items() if r is None)
    if falharam:
        log.warning("Paginas sem resposta nesta rodada: %s (%s). Leio o que veio.",
                    falharam, "; ".join(sorted(set(_causas_falha))[:2]))

    # Remonta na ordem certa; para na primeira pagina que falhou ou
    # veio incompleta (fim do catalogo)
    todos = []
    for numero in range(1, paginas + 1):
        registros = resultados.get(numero)
        if registros is None:
            break
        todos.extend(registros)
        if len(registros) < TAMANHO_PAGINA:
            break

    return todos or None


def montar_item(registro: dict) -> Optional[dict]:
    """
    Converte um produto cru da API no formato que o bot usa.

    Pega os tres dados que voce pediu: titulo, primeira foto e link de compra.
    Preco, categoria e plataforma vem junto de graca.
    """
    produto_id = str(registro.get("id") or "").strip()
    if not produto_id:
        return None

    # Titulo: converte codigos de HTML (&#039 vira apostrofo, etc)
    titulo = html.unescape(str(registro.get("title") or "").strip())
    titulo = re.sub(r"\s+", " ", titulo)
    if not titulo:
        titulo = "(produto sem titulo)"

    # Primeira foto: a imagem da primeira variacao do produto
    skus = registro.get("skus") or []
    primeiro_sku = skus[0] if skus else {}
    imagem = str(primeiro_sku.get("image") or registro.get("thumbnail") or "").strip()

    # Preco: a API devolve em yuan; converte para real tambem
    preco = montar_preco(primeiro_sku.get("price"))

    # Tamanho: vem do sku. Fica vazio em itens que nao sao roupa
    # (servicos, kits) — nesses casos a linha nao aparece na mensagem.
    tamanho = str(primeiro_sku.get("size") or "").strip()

    # Quantidade em estoque. A maioria dos produtos tem UMA unidade so —
    # por isso eles esgotam em minutos, e por isso avisar de um item ja
    # vendido gera reclamacao. Desconhecido conta como disponivel, para
    # nao deixar de avisar por falta de dado.
    try:
        estoque = int(primeiro_sku.get("quantity"))
    except (TypeError, ValueError):
        estoque = None

    return {
        "id": produto_id,                                   # id do proprio site
        "titulo": titulo,
        "titulo_pt": "",                                    # preenchido depois
        "tamanho": tamanho,
        "estoque": estoque,
        "imagem": imagem,
        "link": URL_PRODUTO.format(id=produto_id),          # pagina no CSSDeals
        "compra": URL_COMPRA.format(id=produto_id) + _extra_compra,
        "preco": preco,
        "categoria": CATEGORIAS.get(str(registro.get("categoryId") or ""), ""),
        "grupo": grupo_do_produto(registro.get("categoryId") or ""),
        "plataforma": PLATAFORMAS.get(registro.get("salePlatform"), ""),
        "origem": str(registro.get("sourceLink") or "").strip(),
    }


def extrair_itens(registros: list) -> list:
    """
    Converte a lista crua da API em itens.

    Descarta os invalidos e os ja ESGOTADOS — nao adianta avisar de
    produto sem estoque, so gera clique em link morto.
    """
    itens = []
    esgotados = 0
    for registro in registros:
        item = montar_item(registro)
        if not item:
            continue
        if item.get("estoque") == 0:
            esgotados += 1
            continue
        itens.append(item)

    if esgotados:
        log.info("Ignorados %s produto(s) ja esgotados.", esgotados)
    return itens


# ==========================================================================
#  4.5 MEMORIA EM ARQUIVO DE TEXTO  (para rodar hospedado fora do Mac)
# ==========================================================================
#  Quando o bot roda no GitHub Actions, o computador e apagado ao fim de
#  cada rodada. O banco SQLite nao sobrevive — e, por ser um arquivo
#  binario, versiona-lo a cada 15 minutos incharia o repositorio.
#
#  Entao neste modo a memoria vira um arquivo de texto simples: uma linha
#  por produto ja avisado. Ocupa quase nada, o Git versiona bem e voce
#  consegue abrir e ler se quiser.
# ==========================================================================

# Quantos ids guardar. Precisa ser bem maior que a janela profunda
# (1000 produtos), senao ids esquecidos voltariam a parecer novos.
MAX_IDS_ESTADO = 4000


def carregar_estado(caminho: str) -> list:
    """Le o arquivo de memoria. Se nao existir ainda, devolve lista vazia."""
    if not os.path.exists(caminho):
        return []
    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            return [l.strip() for l in arquivo if l.strip() and not l.startswith("#")]
    except OSError as erro:
        log.warning("Nao consegui ler %s (%s). Comecando do zero.", caminho, erro)
        return []


def salvar_estado(caminho: str, ids: list) -> None:
    """
    Grava a memoria, mantendo so os mais recentes.

    Escreve primeiro num arquivo temporario e so depois substitui o
    definitivo — assim, se faltar energia no meio, o arquivo antigo
    continua intacto em vez de virar lixo.
    """
    recentes = ids[-MAX_IDS_ESTADO:]
    temporario = caminho + ".tmp"
    try:
        with open(temporario, "w", encoding="utf-8") as arquivo:
            arquivo.write("# Produtos que o bot ja avisou. Nao edite a mao.\n")
            arquivo.write("\n".join(recentes) + "\n")
        os.replace(temporario, caminho)
    except OSError as erro:
        log.error("Nao consegui gravar a memoria em %s: %s", caminho, erro)


# ==========================================================================
#  5.3 FOTOS DO ANUNCIO NO CSSDEALS
# ==========================================================================
#  A listagem devolve uma imagem so, e as vezes ela aponta para o site de
#  origem (1688, Taobao, Weidian) em vez do CSSDeals.
#
#  As fotos do anuncio feito no CSSDeals so aparecem no endpoint de
#  detalhe. Buscamos de la a foto escolhida (por padrao a segunda).
#
#  Isso e feito SO para os produtos que serao avisados — nunca para os
#  1000 da varredura profunda.
# ==========================================================================

def ainda_disponivel(produto_id: str) -> Optional[bool]:
    """
    Confere o estoque NA HORA, rente ao envio.

    Por que existe: aplicar_fotos confere o estoque de TODOS os itens de
    uma vez, em paralelo (rapido). Mas o ENVIO e sequencial, com pausa
    entre mensagens para nao estourar o limite do Telegram — numa leva
    de 15 itens, o 15o e enviado ~18s depois do 1o. Como a maioria dos
    produtos tem 1 unidade so, 18s e tempo de sobra para esgotar entre a
    conferencia em lote e a entrega de verdade. Esta funcao reconfere
    IMEDIATAMENTE antes de cada envio, fechando essa janela.

    Devolve True (tem estoque), False (esgotado) ou None (nao deu para
    confirmar — nesse caso o chamador deve enviar mesmo assim, para nao
    perder o aviso por causa de uma consulta que falhou).
    """
    detalhe = buscar_detalhe(produto_id)
    if detalhe is REMOVIDO:
        return False          # saiu do catalogo: nao existe mais para comprar
    if detalhe is None:
        return None
    sku = (detalhe.get("skus") or [{}])[0]
    try:
        return int(sku.get("quantity")) != 0
    except (TypeError, ValueError):
        return None


# Devolvido por buscar_detalhe quando o site diz, DEFINITIVAMENTE, que o
# produto nao existe mais ("The details of the specified product do not
# exist", code 404). Nao e o mesmo que falha de rede (None): falha de rede
# significa "nao sei", e o bot envia mesmo assim; REMOVIDO significa "sei
# que acabou", e o bot NAO pode avisar — o cliente cairia numa pagina vazia.
REMOVIDO = {"_removido": True}


def buscar_detalhe(produto_id: str):
    """
    Busca o detalhe do produto (fotos + estoque atual) numa so chamada.

    Devolve: dict com os dados | REMOVIDO (produto saiu do catalogo) |
    None (nao deu para confirmar: rede, timeout, resposta estranha).
    """
    try:
        resposta = requests.get(API_DETALHE.format(id=produto_id),
                                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resposta.raise_for_status()
        corpo = resposta.json()
    except Exception as erro:
        log.warning("Nao consegui o detalhe de %s (%s).", produto_id, str(erro)[:60])
        return None
    codigo = corpo.get("code")
    if codigo == 0:
        return corpo.get("data") or {}
    if codigo == 404:
        return REMOVIDO
    return None


def buscar_foto(produto_id: str, sessao: Optional[requests.Session] = None) -> Optional[str]:
    """
    Pega a foto do anuncio no CSSDeals (a segunda, por padrao).

    Se o detalhe falhar ou o produto tiver so uma foto, devolve o que der
    — e quem chamou mantem a imagem antiga. Nunca deixa de notificar por
    causa de foto.
    """
    try:
        pedir = (sessao or requests).get
        resposta = pedir(API_DETALHE.format(id=produto_id),
                         headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resposta.raise_for_status()
        corpo = resposta.json()
    except Exception as erro:
        log.warning("Nao consegui as fotos de %s (%s).", produto_id, str(erro)[:60])
        return None

    if corpo.get("code") != 0:
        return None

    fotos = ((corpo.get("data") or {}).get("images")) or []
    enderecos = [f.get("url") for f in fotos if f.get("url")]
    if not enderecos:
        return None

    # Pede a foto escolhida; se o produto tiver menos fotos que isso,
    # fica com a ultima que existe
    escolhida = (enderecos[_foto_escolhida]
                 if _foto_escolhida < len(enderecos) else enderecos[-1])
    return escolhida + REDIMENSIONA_FOTO


def aplicar_fotos(itens: list, conexao=None) -> list:
    """
    Busca a foto do anuncio E reconfere o estoque, numa chamada so.

    Devolve apenas os itens AINDA DISPONIVEIS. Uma leva grande leva
    dezenas de segundos para ser enviada, e a maioria dos produtos tem
    uma unidade — entao da tempo de esgotar enquanto esperam na fila.
    Avisar de item vendido e o que mais gera reclamacao.
    """
    if not itens:
        return itens

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as executor:
        detalhes = list(executor.map(lambda i: buscar_detalhe(i["id"]), itens))

    disponiveis, trocadas, vendidos = [], 0, 0

    for item, detalhe in zip(itens, detalhes):
        # Produto REMOVIDO do catalogo: nao existe mais. Descarta e marca
        # como concluido — antes, isso era confundido com falha de rede e o
        # bot avisava de produto que o cliente nao conseguia abrir.
        if detalhe is REMOVIDO:
            vendidos += 1
            if conexao is not None:
                marcar_notificado(conexao, item["id"])
            continue

        # Sem detalhe (falha de rede): mantem o item, para nao deixar de
        # avisar por causa de uma consulta que nao respondeu.
        if detalhe is None:
            disponiveis.append(item)
            continue

        skus = detalhe.get("skus") or []
        primeiro = skus[0] if skus else {}
        try:
            estoque = int(primeiro.get("quantity"))
        except (TypeError, ValueError):
            estoque = None

        if estoque == 0:
            vendidos += 1
            if conexao is not None:
                # Marca como avisado para nao tentar de novo na proxima rodada
                marcar_notificado(conexao, item["id"])
            continue

        fotos = [f.get("url") for f in (detalhe.get("images") or []) if f.get("url")]
        if fotos:
            escolhida = (fotos[_foto_escolhida] if _foto_escolhida < len(fotos)
                         else fotos[-1]) + REDIMENSIONA_FOTO
            if escolhida != item.get("imagem"):
                item["imagem"] = escolhida
                trocadas += 1
                if conexao is not None:
                    conexao.execute("UPDATE itens SET imagem = ? WHERE id = ?",
                                    (escolhida, item["id"]))
        disponiveis.append(item)

    if conexao is not None:
        conexao.commit()
    if trocadas:
        log.info("Foto do anuncio do CSSDeals aplicada em %s item(ns).", trocadas)
    if vendidos:
        log.info("%s item(ns) esgotaram enquanto esperavam na fila — nao avisados.",
                 vendidos)
    return disponiveis


# ==========================================================================
#  5.4 COTACAO: CONVERTER YUAN PARA REAL
# ==========================================================================
#  Os precos do site vem em Yuan chines. Aqui viram reais, para voce nao
#  precisar fazer a conta de cabeca a cada anuncio.
# ==========================================================================

# Guarda a ultima cotacao e a hora em que foi buscada
_cotacao = {"valor": None, "quando": 0.0}


def cotacao_cny_brl() -> Optional[float]:
    """
    Quanto vale 1 Yuan em reais.

    Busca uma vez por hora e reaproveita. Se as duas fontes falharem,
    devolve None — e o bot mostra so o preco em Yuan, sem quebrar.
    """
    agora = time.time()
    if _cotacao["valor"] and (agora - _cotacao["quando"]) < VALIDADE_COTACAO:
        return _cotacao["valor"]

    for nome, url in FONTES_COTACAO:
        try:
            resposta = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
            resposta.raise_for_status()
            corpo = resposta.json()

            if "awesomeapi" in url:
                valor = float(corpo["CNYBRL"]["bid"])
            else:
                valor = float(corpo["rates"]["BRL"])

            # Sanidade: se vier um numero absurdo, e sinal de que a
            # fonte mudou o formato — melhor ignorar do que mostrar
            # um preco errado para voce.
            if not (0.1 < valor < 10):
                log.warning("Cotacao suspeita da %s: %s. Ignorando.", nome, valor)
                continue

            _cotacao["valor"] = valor
            _cotacao["quando"] = agora
            log.info("Cotacao (%s): 1 CN¥ = R$ %.4f", nome, valor)
            return valor

        except Exception as erro:
            log.warning("Fonte de cotacao %s falhou: %s", nome, str(erro)[:80])

    log.warning("Nenhuma fonte de cotacao respondeu. Mostrando so em Yuan.")
    return None


def formatar_numero(valor: float) -> str:
    """Formata no padrao brasileiro: 1.234,56 em vez de 1,234.56."""
    texto = "{:,.2f}".format(valor)
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def montar_preco(valor_yuan) -> str:
    """
    Monta o texto do preco: Yuan e, quando possivel, o valor em reais.

    Exemplo:  CN¥ 30,19  (~R$ 23,12)
    """
    if valor_yuan is None:
        return ""

    try:
        yuan = float(valor_yuan)
    except (TypeError, ValueError):
        return ""

    texto = "CN¥ {}".format(formatar_numero(yuan))

    # So consulta a cotacao se voce tiver pedido a conversao
    if _mostrar_real:
        taxa = cotacao_cny_brl()
        if taxa:
            texto += "  (~R$ {})".format(formatar_numero(yuan * taxa))

    return texto


# ==========================================================================
#  5.5 TRADUCAO DOS TITULOS
# ==========================================================================
#  Os produtos vem do Taobao/Weidian/1688, entao os titulos chegam em
#  chines ou em ingles. Aqui eles viram portugues.
#
#  Cada traducao e guardada no banco. Se o mesmo titulo aparecer de novo,
#  o bot usa a traducao salva em vez de pedir outra vez — isso economiza
#  a cota do servico gratuito e deixa tudo mais rapido.
# ==========================================================================

# Fica True se a cota diaria acabar, para nao insistir a rodada inteira
_traducao_indisponivel = False


def criar_cache_traducao(conexao: sqlite3.Connection) -> None:
    """Cria a tabelinha que guarda as traducoes ja feitas."""
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS traducoes (
            original   TEXT PRIMARY KEY,
            traduzido  TEXT NOT NULL
        )
        """
    )
    conexao.commit()


# Cache em memoria, usado quando nao ha banco (modo GitHub Actions)
_cache_memoria = {}


def traducao_no_cache(conexao, texto: str) -> Optional[str]:
    if conexao is None:
        return _cache_memoria.get(texto)
    linha = conexao.execute(
        "SELECT traduzido FROM traducoes WHERE original = ?", (texto,)
    ).fetchone()
    return linha[0] if linha else None


def guardar_traducao(conexao, texto: str, traduzido: str) -> None:
    if conexao is None:
        _cache_memoria[texto] = traduzido
        return
    conexao.execute(
        "INSERT OR REPLACE INTO traducoes (original, traduzido) VALUES (?, ?)",
        (texto, traduzido),
    )
    conexao.commit()


def detectar_idioma(texto: str) -> str:
    """
    Descobre se o titulo esta em chines ou em ingles.

    Simples e eficaz: se tiver ideograma chines no meio, e chines.
    """
    for caractere in texto:
        if "\u4e00" <= caractere <= "\u9fff":   # faixa dos ideogramas chineses
            return "zh-CN"
    return "en"


def traduzir(texto: str, conexao, email: str = "") -> str:
    """
    Traduz um titulo para portugues.

    Se qualquer coisa der errado (sem internet, cota esgotada, servico fora
    do ar), devolve o titulo ORIGINAL em vez de quebrar. Voce nunca deixa de
    receber a notificacao por causa da traducao.
    """
    global _traducao_indisponivel

    texto = (texto or "").strip()
    if not texto:
        return texto

    # 1) Ja traduzimos esse titulo antes?
    salva = traducao_no_cache(conexao, texto)
    if salva is not None:
        return salva

    # 2) A cota acabou nesta rodada? Nao adianta tentar de novo.
    if _traducao_indisponivel:
        return texto

    recorte = texto[:LIMITE_TEXTO_TRADUCAO]
    parametros = {
        "q": recorte,
        "langpair": "{}|{}".format(detectar_idioma(texto), IDIOMA_DESTINO),
    }
    if email:
        parametros["de"] = email      # informar um e-mail aumenta a cota diaria

    try:
        resposta = requests.get(
            API_TRADUCAO, params=parametros,
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        resposta.raise_for_status()
        corpo = resposta.json()
    except Exception as erro:
        log.warning("Traducao falhou (%s). Mantendo o titulo original.", erro)
        return texto

    # Cota diaria estourada
    if corpo.get("quotaFinished"):
        _traducao_indisponivel = True
        log.warning(
            "A cota diaria de traducao acabou. Os titulos continuam chegando, "
            "so que no idioma original. Volta ao normal amanha. "
            "(Dica: preencher TRADUCAO_EMAIL no .env aumenta bastante a cota.)"
        )
        return texto

    if corpo.get("responseStatus") != 200:
        log.warning(
            "Servico de traducao recusou: %s. Mantendo o titulo original.",
            str(corpo.get("responseDetails"))[:100],
        )
        return texto

    traduzido = str((corpo.get("responseData") or {}).get("translatedText") or "").strip()
    if not traduzido:
        return texto

    # O MyMemory as vezes devolve um aviso em vez da traducao
    if "QUERY LENGTH LIMIT" in traduzido.upper() or "INVALID" in traduzido.upper():
        return texto

    guardar_traducao(conexao, texto, traduzido)
    time.sleep(DELAY_ENTRE_TRADUCOES)
    return traduzido


# ==========================================================================
#  6. NOTIFICACOES — Telegram e Discord
# ==========================================================================

def titulo_visivel(item: dict) -> str:
    """Titulo em portugues quando existe; senao, o original."""
    return (item.get("titulo_pt") or "").strip() or item["titulo"]


def montar_texto_telegram(item: dict) -> str:
    """Monta a mensagem no formato HTML do Telegram (negrito, link clicavel)."""
    linhas = ["\U0001F195 <b>{}</b>".format(escapar_html(titulo_visivel(item)))]

    # Tamanho vem logo abaixo do nome. Se o produto nao tiver, a linha
    # simplesmente nao aparece.
    if item.get("tamanho"):
        linhas.append("Tamanho: <b>{}</b>".format(escapar_html(item["tamanho"])))

    # Mostra o titulo original tambem — util para procurar o produto no site
    original = item["titulo"]
    if original and original != titulo_visivel(item):
        linhas.append("<i>{}</i>".format(escapar_html(original)))

    etiquetas = [e for e in (item.get("categoria"), item.get("plataforma")) if e]
    if etiquetas:
        linhas.append(escapar_html(" · ".join(etiquetas)))

    if item.get("preco"):
        linhas.append("Preco: <b>{}</b>".format(escapar_html(item["preco"])))

    # Uma linha em branco e um tracejado separam o link do resto,
    # para ele nao competir com o preco pela atencao.
    # O link de compra fica destacado entre duas linhas, sozinho —
    # e a acao principal. O link do CSSDeals vem depois, secundario.
    risco = "—" * 18
    if item.get("compra"):
        linhas.append(risco)
        linhas.append('  🛒 <a href="{}"><b>COMPRE AQUI</b></a>'.format(item["compra"]))
    if item.get("link"):
        linhas.append(risco)
        linhas.append('<a href="{}">Ver no CSSDeals</a>'.format(item["link"]))

    return "\n".join(linhas)


def escapar_html(texto: str) -> str:
    """Protege caracteres especiais para nao quebrar a formatacao do Telegram."""
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def enviar_telegram(item: dict, token: str, chat_id: str) -> bool:
    """
    Envia UM item para o grupo do Telegram.

    Se o item tem foto, manda a foto com legenda. Se nao tem (ou se a foto
    falhar), manda so o texto. Devolve True se conseguiu enviar.
    """
    texto = montar_texto_telegram(item)
    base = f"https://api.telegram.org/bot{token}"

    # Tentativa 1: mandar com a foto
    if item["imagem"]:
        ok = _post_telegram(
            f"{base}/sendPhoto",
            {
                "chat_id": chat_id,
                "photo": item["imagem"],
                "caption": texto,
                "parse_mode": "HTML",
            },
        )
        if ok:
            return True
        log.warning("Nao deu para mandar a foto. Tentando so com texto...")

    # Tentativa 2 (ou unica): so texto
    return _post_telegram(
        f"{base}/sendMessage",
        {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


def _post_telegram(url: str, dados: dict) -> bool:
    """
    Faz o envio de fato e trata os erros SEM derrubar o script.

    Erros tratados:
      - 429 (rate limit): espera o tempo que o Telegram pedir e tenta de novo
      - 403 (bot sem permissao no grupo): avisa no log e segue
      - qualquer outro: loga e segue
    """
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resposta = requests.post(url, data=dados, timeout=(5, TIMEOUT))

            if resposta.status_code == 200:
                return True

            corpo = resposta.json() if resposta.content else {}
            descricao = corpo.get("description", resposta.text[:200])

            # Rate limit: o Telegram diz quantos segundos esperar
            if resposta.status_code == 429:
                espera = corpo.get("parameters", {}).get("retry_after", 5)
                log.warning("Limite do Telegram atingido. Esperando %ss...", espera)
                time.sleep(espera + 1)
                continue

            # Bot sem permissao / expulso do grupo — nao adianta insistir
            if resposta.status_code in (401, 403):
                log.error(
                    "TELEGRAM SEM PERMISSAO (%s): %s "
                    "-> Confira se o bot foi adicionado ao grupo e se o "
                    "TELEGRAM_CHAT_ID esta correto.",
                    resposta.status_code, descricao,
                )
                return False

            if resposta.status_code == 400:
                # Caso especial: o grupo virou supergrupo e trocou de ID.
                # O Telegram informa o ID novo na propria resposta — vamos
                # mostra-lo, em vez de deixar voce procurando.
                novo_id = corpo.get("parameters", {}).get("migrate_to_chat_id")
                if novo_id:
                    log.error("=" * 60)
                    log.error("O GRUPO VIROU SUPERGRUPO E MUDOU DE ID.")
                    log.error("")
                    log.error("   ID antigo (o que esta configurado): %s", dados.get("chat_id"))
                    log.error("   ID NOVO (use este):                 %s", novo_id)
                    log.error("")
                    log.error("   Troque TELEGRAM_CHAT_ID para %s", novo_id)
                    log.error("   nas Variables do Railway. E so isso.")
                    log.error("=" * 60)
                else:
                    log.error("TELEGRAM recusou a mensagem (400): %s", descricao)
                return False

            log.warning(
                "Telegram devolveu erro %s (tentativa %s/%s): %s",
                resposta.status_code, tentativa, MAX_TENTATIVAS, descricao,
            )
            time.sleep(2 * tentativa)

        except requests.exceptions.RequestException as erro:
            log.warning(
                "Falha de rede ao falar com o Telegram (tentativa %s/%s): %s",
                tentativa, MAX_TENTATIVAS, erro,
            )
            time.sleep(2 * tentativa)

    log.error("Desisti de enviar esta mensagem no Telegram apos %s tentativas.", MAX_TENTATIVAS)
    return False


# Cada webhook do Discord aceita ~5 mensagens a cada 2 segundos (30 por minuto).
# O envio em rajada paralelo (ate 10 itens ao mesmo tempo no MESMO webhook)
# estourava esse limite numa leva grande: o Discord respondia 429 e o bot desistia
# do item depois de 3 tentativas ("Discord NAO recebeu"). Agora cada webhook tem
# uma fila propria com um intervalo minimo entre mensagens; webhooks diferentes
# (canal geral x canal da categoria) continuam em paralelo.
INTERVALO_WEBHOOK = 0.5
_webhook_guarda = threading.Lock()
_webhook_trava = {}
_webhook_ultimo = {}


def _esperar_vez_do_webhook(url: str) -> None:
    with _webhook_guarda:
        trava = _webhook_trava.setdefault(url, threading.Lock())
    with trava:
        falta = _webhook_ultimo.get(url, 0.0) + INTERVALO_WEBHOOK - time.time()
        if falta > 0:
            time.sleep(falta)
        _webhook_ultimo[url] = time.time()


def enviar_discord(item: dict, webhook_url: str) -> bool:
    """
    Envia UM item para o canal do Discord usando um Webhook.

    O Discord monta um card bonito (embed) com titulo, link e foto.
    """
    embed = {
        "title": titulo_visivel(item)[:250],
        "color": 0x00B37E,   # verdinho
    }
    if item.get("link"):
        embed["url"] = item["link"]
    if item.get("imagem"):
        embed["image"] = {"url": item["imagem"]}

    detalhes = []
    if item.get("tamanho"):
        detalhes.append("**Tamanho:** {}".format(item["tamanho"]))
    original = item["titulo"]
    if original and original != titulo_visivel(item):
        detalhes.append("*{}*".format(original[:200]))
    if item.get("preco"):
        detalhes.append("**{}**".format(item["preco"]))
    etiquetas = [e for e in (item.get("categoria"), item.get("plataforma")) if e]
    if etiquetas:
        detalhes.append(" · ".join(etiquetas))
    if item.get("compra"):
        detalhes.append("[🛒 **COMPRE AQUI**]({})  ·  [Ver no CSSDeals]({})".format(
            item["compra"], item.get("link", "")))
    if detalhes:
        embed["description"] = "\n".join(detalhes)

    tentativa = 0
    limitados = 0
    while tentativa < MAX_TENTATIVAS:
        tentativa += 1
        try:
            _esperar_vez_do_webhook(webhook_url)
            resposta = requests.post(
                webhook_url, json={"embeds": [embed]}, timeout=(5, TIMEOUT)
            )

            if resposta.status_code in (200, 204):
                return True

            # Rate limit do Discord
            if resposta.status_code == 429:
                corpo = resposta.json() if resposta.content else {}
                espera = float(corpo.get("retry_after", 5))
                limitados += 1
                if limitados <= 8:
                    tentativa -= 1      # limite de taxa nao e erro: nao gasta tentativa
                log.warning("Limite do Discord atingido (%sa vez). Esperando %.1fs...",
                            limitados, espera)
                time.sleep(espera + 0.2)
                continue

            # Conteudo recusado (ex.: foto com endereco invalido). Melhor avisar
            # sem a foto do que nao avisar: tira a imagem e, se preciso, o link.
            if resposta.status_code == 400:
                log.warning("Discord recusou o conteudo (400): %s", resposta.text[:300])
                if "image" in embed:
                    embed.pop("image")
                    continue
                if "url" in embed:
                    embed.pop("url")
                    continue
                break

            if resposta.status_code in (401, 403, 404):
                log.error(
                    "DISCORD recusou (%s) -> a URL do webhook parece invalida "
                    "ou foi apagada. Gere um webhook novo no canal.",
                    resposta.status_code,
                )
                return False

            log.warning(
                "Discord devolveu erro %s (tentativa %s/%s): %s",
                resposta.status_code, tentativa, MAX_TENTATIVAS, resposta.text[:500],
            )
            time.sleep(2 * tentativa)

        except requests.exceptions.RequestException as erro:
            log.warning(
                "Falha de rede ao falar com o Discord (tentativa %s/%s): %s",
                tentativa, MAX_TENTATIVAS, erro,
            )
            time.sleep(2 * tentativa)

    # Ao desistir, grava o embed EXATO que foi recusado — sem isso, uma
    # rejeicao de conteudo (Discord ser chato com algum campo) e
    # impossivel de diagnosticar depois, porque o log normal so mostra
    # o erro do Discord, nao o que o bot tentou mandar.
    log.error("Desisti de enviar esta mensagem no Discord apos %s tentativas.", MAX_TENTATIVAS)
    log.error("Embed recusado (para diagnostico): %s", json.dumps(embed, ensure_ascii=False)[:800])
    return False


def notificar(item: dict, config: dict) -> bool:
    """
    Manda o item para todos os canais configurados.

    Se voce preencheu so o Telegram, vai so pro Telegram. Se preencheu so o
    Discord, vai so pro Discord. Se preencheu os dois, vai pros dois.
    """
    enviou_algum = False

    if config["telegram_token"] and config["telegram_chat_id"]:
        if enviar_telegram(item, config["telegram_token"], config["telegram_chat_id"]):
            enviou_algum = True

    # Canal geral: recebe tudo, se estiver configurado
    if config["discord_webhook"]:
        if enviar_discord(item, config["discord_webhook"]):
            enviou_algum = True

    # Canal do grupo do produto (calcados, roupas, ...)
    canal = config.get("canais", {}).get(item.get("grupo", ""))
    if canal:
        if enviar_discord(item, canal):
            enviou_algum = True

    return enviou_algum


# ==========================================================================
#  7. CONFIGURACAO (.env)
# ==========================================================================

def _inteiro_do_ambiente(nome: str, padrao: int, minimo: int = 10) -> int:
    """Le um numero do .env; se estiver vazio ou escrito errado, usa o padrao."""
    bruto = os.getenv(nome, "").strip()
    if not bruto:
        return padrao
    try:
        valor = int(bruto)
    except ValueError:
        log.warning("%s='%s' nao e um numero. Usando %s.", nome, bruto, padrao)
        return padrao
    if valor < minimo:
        log.warning("%s=%s e agressivo demais com o site. Usando %s.",
                    nome, valor, minimo)
        return minimo
    return valor


def carregar_config() -> dict:
    """Le o arquivo .env e confere se pelo menos um canal foi configurado."""
    load_dotenv()

    config = {
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        "discord_webhook": os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        # Um webhook por grupo de categoria (CANAL_CALCADOS, CANAL_ROUPAS...)
        "canais": {g: os.getenv("CANAL_" + g, "").strip()
                   for g in GRUPOS if os.getenv("CANAL_" + g, "").strip()},
        # Vazio = monitora lancamentos de TODAS as abas do site.
        # Preenchido = so daquela aba (ex: 11 para Shoes, 32 para Hoodie).
        "categoria": os.getenv("CATEGORIA_ID", "").strip(),
        # Traduzir os titulos para portugues? (sim por padrao)
        # Desligada por padrao: os titulos vao como o site publica.
        # Para ligar, use TRADUZIR=sim.
        "traduzir": os.getenv("TRADUZIR", "nao").strip().lower()
                    in ("sim", "yes", "1", "true"),
        # E-mail opcional: aumenta a cota diaria gratuita de traducao
        "traducao_email": os.getenv("TRADUCAO_EMAIL", "").strip(),
        # Se preenchido, usa arquivo de texto no lugar do banco SQLite.
        # E o modo usado quando o bot roda hospedado (GitHub Actions).
        "arquivo_estado": os.getenv("ARQUIVO_ESTADO", "").strip(),
        "intervalo": _inteiro_do_ambiente("INTERVALO_SEGUNDOS", INTERVALO_PADRAO),
        "varredura_seg": _inteiro_do_ambiente(
            "SEGUNDOS_ENTRE_VARREDURAS", VARREDURA_PADRAO_SEG, minimo=5),
        "profunda_seg": _inteiro_do_ambiente(
            "VARREDURA_PROFUNDA_SEG", VARREDURA_PROFUNDA_PADRAO_SEG, minimo=5),
        "mostrar_real": os.getenv("MOSTRAR_REAL", "nao").strip().lower()
                        in ("sim", "yes", "1", "true"),
        # 0 = primeira foto do anuncio, 1 = segunda, e assim por diante
        "foto": _inteiro_do_ambiente("FOTO_DO_ANUNCIO", FOTO_PADRAO, minimo=0),
        "pico_inicio": _para_minutos(os.getenv("PICO_INICIO", PICO_INICIO_PADRAO), PICO_INICIO_PADRAO),
        "pico_fim": _para_minutos(os.getenv("PICO_FIM", PICO_FIM_PADRAO), PICO_FIM_PADRAO),
        "pico_segundos": _inteiro_do_ambiente("PICO_SEGUNDOS", PICO_SEGUNDOS_PADRAO, minimo=5),
        "fuso": int(os.getenv("FUSO_HORAS", str(FUSO_PADRAO)) or FUSO_PADRAO),
        "cssbuy_extra": os.getenv("CSSBUY_EXTRA", "").strip(),
        # Quantas horas para tras recuperar na PRIMEIRA rodada (0 = nenhuma).
        "recuperar_horas": _inteiro_do_ambiente(
            "RECUPERAR_HORAS",
            RECUPERACAO_UNICA_HORAS if datetime.now(timezone.utc) < RECUPERACAO_UNICA_ATE else 0,
            minimo=0),
        # Quantos dos produtos mais novos (com estoque) anunciar na primeira rodada.
        "enviar_ultimos": _inteiro_do_ambiente(
            "ENVIAR_ULTIMOS",
            ENVIO_MANUAL_QTD if datetime.now(timezone.utc) < ENVIO_MANUAL_ATE else 0,
            minimo=0),
    }

    tem_telegram = bool(config["telegram_token"] and config["telegram_chat_id"])
    tem_discord = bool(config["discord_webhook"]) or bool(config["canais"])

    if not tem_telegram and not tem_discord:
        log.error(
            "Nenhum canal de notificacao configurado!\n"
            "   Abra o arquivo .env e preencha OU o Telegram "
            "(TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) OU o Discord "
            "(DISCORD_WEBHOOK_URL).\n"
            "   O passo a passo esta no README.md."
        )
        sys.exit(1)

    canais = []
    if tem_telegram:
        canais.append("Telegram")
    if tem_discord:
        canais.append("Discord (canal geral)")
    for g in config["canais"]:
        canais.append("Discord/" + NOME_GRUPO.get(g, g))
    log.info("Canais ativos: %s", " + ".join(canais))

    if config["categoria"]:
        nome = CATEGORIAS.get(config["categoria"], "categoria " + config["categoria"])
        log.info("Monitorando SOMENTE a aba: %s", nome)
    else:
        log.info("Monitorando lancamentos de TODAS as abas do site.")

    global _mostrar_real, _foto_escolhida, _extra_compra
    _mostrar_real = config["mostrar_real"]
    _foto_escolhida = config["foto"]

    # Garante que comece com & ou ?, para nao grudar no parametro anterior
    extra = config["cssbuy_extra"]
    if extra and not extra.startswith(("&", "?")):
        extra = "&" + extra
    _extra_compra = extra
    if extra:
        log.info("Codigo de indicacao do CSSBuy ativo nos links de compra.")
    else:
        log.info("Sem codigo de indicacao — links de compra sem comissao.")
    log.info("Foto usada nos avisos: a %sa do anuncio.", _foto_escolhida + 1)
    log.info("Precos em Yuan%s.", " + reais" if _mostrar_real else " (CN¥)")
    from datetime import timedelta as _td
    def _hhmm(m): return "%02d:%02d" % (m // 60, m % 60)
    log.info("Leitura rapida a cada ~%ss; varredura profunda (10 paginas) a cada %ss.",
             config["varredura_seg"], config["profunda_seg"])
    log.info("HORARIO DE PICO %s as %s (fuso %+d): varredura a cada %ss.",
             _hhmm(config["pico_inicio"]), _hhmm(config["pico_fim"]),
             config["fuso"], config["pico_segundos"])
    log.info("Agora sao %s no seu fuso — pico %sATIVO.",
             _hora_local(config["fuso"]).strftime("%H:%M"),
             "" if em_horario_de_pico(config) else "NAO ")

    if config["traduzir"]:
        log.info("Traducao dos titulos para portugues: LIGADA.")
    else:
        log.info("Traducao: desligada — titulos como o site publica.")

    return config


# ==========================================================================
#  8. RODADA DE COLETA
# ==========================================================================

def escolher_para_envio_manual(itens: list, quantos: int) -> list:
    """
    Escolhe os `quantos` produtos mais novos que ainda existem e tem estoque.

    Confere cada candidato no site (a lista pode estar alguns segundos atrasada
    e a maioria dos produtos tem 1 unidade). Devolve do mais ANTIGO para o mais
    novo, para o canal terminar com o mais recente embaixo.
    """
    escolhidos = []
    for item in sorted(itens, key=lambda i: int(i["id"]), reverse=True):
        if len(escolhidos) >= quantos:
            break
        if ainda_disponivel(item["id"]) is not False:    # None = nao deu para saber: mantem
            escolhidos.append(item)
    return escolhidos[::-1]


def rodar_coleta(config: dict) -> None:
    """Executa UMA rodada: pergunta os lancamentos, salva e avisa os novos."""
    if time.time() < _bloqueio["ate"] or time.time() < _falha_rede["ate"]:
        return          # site barrando/sem responder: recua em silencio ate o prazo

    inicio = time.time()
    log.info("=" * 60)
    log.info("Procurando lancamentos novos em %s", SITE_BASE)

    # Passo 0: pedir licenca ao robots.txt antes de qualquer acesso
    if not robots_permite(API_PRODUTOS):
        return

    conexao = abrir_banco()
    criar_cache_traducao(conexao)
    sessao = obter_sessao()
    primeira_vez = banco_vazio(conexao)

    # Passo 1: buscar os produtos mais recentes na API do site
    pico = em_horario_de_pico(config)
    paginas, profunda = paginas_desta_rodada(primeira_vez, config["profunda_seg"])
    if profunda:
        log.info(
            "Varredura PROFUNDA%s: lendo %s paginas (~%s produtos) para achar "
            "itens que ficaram visiveis agora mas foram criados ha dias.",
            " (HORARIO DE PICO)" if pico else "", paginas, paginas * TAMANHO_PAGINA,
        )

    registros = buscar_lancamentos(sessao, config["categoria"], paginas)
    if registros is None:
        log.warning("Rodada sem leitura do site (veja a causa acima).")
        conexao.close()
        return

    # Passo 2: converter para o formato do bot
    itens = extrair_itens(registros)
    log.info("Produtos lidos nesta rodada: %s", len(itens))

    if not itens:
        log.warning(
            "Nenhum produto veio na resposta. Ou o site esta sem novidades, "
            "ou a API mudou — me chame para ajustar."
        )
        conexao.close()
        return

    # Passo 3: PRIMEIRA RODADA — so registra a base, sem notificar.
    # Sem isso voce receberia 50 mensagens de uma vez logo de cara, de
    # produtos que ja estavam no site antes de voce ligar o bot.
    if primeira_vez:
        # Reiniciar o bot (deploy, queda, bloqueio longo) apaga a memoria e a
        # primeira rodada trata tudo o que existe como "ja visto" — ou seja,
        # engole o que foi publicado enquanto ele estava fora. RECUPERAR_HORAS
        # deixa os itens criados nas ultimas N horas na fila para serem
        # anunciados (o estoque e reconferido antes de cada envio).
        # Use so apos uma parada; depois apague a variavel, senao um deploy
        # comum reanunciaria itens que ja tinham sido avisados.
        corte = None
        if config.get("recuperar_horas"):
            corte = datetime.now(timezone.utc) - timedelta(hours=config["recuperar_horas"])
            log.info("RECUPERACAO ativa: itens criados nas ultimas %s horas serao "
                     "anunciados (se ainda tiverem estoque).", config["recuperar_horas"])
        manuais = []
        if config.get("enviar_ultimos"):
            manuais = escolher_para_envio_manual(itens, config["enviar_ultimos"])
            log.info("ENVIO MANUAL: %s produto(s) mais novos, com estoque, entram na fila.",
                     len(manuais))
        # Lista fixa do segundo envio manual (so dentro do prazo): do mais antigo ao
        # mais novo, ATRAS dos lancamentos normais e com validade estendida.
        fixos = []
        if datetime.now(timezone.utc) < ENVIO_MANUAL_IDS_ATE:
            lista = set(ENVIO_MANUAL_IDS)
            fixos = sorted((i for i in itens if i["id"] in lista), key=lambda i: int(i["id"]))
            log.info("ENVIO MANUAL (lista fixa): %s de %s produtos estao no catalogo com estoque.",
                     len(fixos), len(lista))
        ids_manuais = {m["id"] for m in manuais} | {f["id"] for f in fixos}
        recuperados = 0
        for item in itens:
            if item["id"] in ids_manuais:
                continue                       # entra abaixo, na ordem certa
            recente = corte is not None and (
                EPOCH_ID + timedelta(milliseconds=int(item["id"]) >> 22)) >= corte
            if recente:
                recuperados += 1
            salvar_item(conexao, item, ja_notificado=not recente)
        for item in manuais:                   # do mais antigo ao mais novo
            salvar_item(conexao, item, ja_notificado=False)
        if fixos:
            # visto_em no futuro: fica atras de qualquer item novo e nao expira
            # antes de a fila inteira (limite do Telegram ~20/min) ser enviada.
            atras = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
            for item in fixos:
                salvar_item(conexao, item, ja_notificado=False, visto_em=atras)
        if recuperados:
            log.info("%s item(ns) recente(s) ficaram na fila para serem anunciados.", recuperados)
        piso = guardar_piso(conexao, [i["id"] for i in itens])
        log.info("Janela de referencia: produtos criados desde %s.",
                 (EPOCH_ID + timedelta(milliseconds=piso >> 22)).astimezone(
                     timezone(timedelta(hours=-3))).strftime("%d/%m/%Y"))
        conexao.close()
        log.info(
            "PRIMEIRA RODADA: guardei %s produtos como ponto de partida, "
            "sem enviar mensagem.", len(itens),
        )
        log.info(
            "A partir de agora voce so sera avisado do que for LANCADO "
            "depois deste momento. Pode deixar o bot rodando."
        )
        return

    # Passo 4: separar o que e realmente novo
    piso = obter_piso(conexao)
    novos = []
    deslizaram = 0
    for item in itens:
        if item_ja_existe(conexao, item["id"]):
            continue
        if piso and int(item["id"]) < piso:
            # Mais velho que tudo o que o bot viu na partida: so entrou na
            # janela porque o catalogo encolheu. NAO e lancamento.
            salvar_item(conexao, item, ja_notificado=True)
            deslizaram += 1
            continue
        salvar_item(conexao, item)      # salva na hora (incremental)
        novos.append(item)

    if deslizaram:
        log.info("Ignorados %s produto(s) ANTIGOS que entraram na janela por baixo "
                 "(o catalogo encolheu) — nao sao lancamentos.", deslizaram)
    log.info("LANCAMENTOS NOVOS nesta rodada: %s", len(novos))

    # Passo 4.5: traduzir os titulos dos novos para portugues.
    # So os NOVOS sao traduzidos — os 50 da primeira rodada nao gastam cota.
    if novos and config["traduzir"]:
        log.info("Traduzindo %s titulo(s) para portugues...", len(novos))
        for item in novos:
            item["titulo_pt"] = traduzir(
                item["titulo"], conexao, config["traducao_email"]
            )
            conexao.execute(
                "UPDATE itens SET titulo_pt = ? WHERE id = ?",
                (item["titulo_pt"], item["id"]),
            )
        conexao.commit()

    # Aviso util: se TODOS os produtos lidos forem novos, e sinal de que
    # sairam mais lancamentos do que o bot consegue ver por rodada.
    if novos and len(novos) == len(itens) and not profunda:
        log.warning(
            "Todos os %s produtos lidos eram novos — pode ter escapado algum. "
            "Se isso repetir, diminua o INTERVALO_SEGUNDOS.", len(itens),
        )

    # Passo 5: notificar tudo que ainda nao foi avisado.
    # Inclui os novos de agora E qualquer atrasado de rodadas anteriores
    # (por exemplo, se o Discord estava fora do ar ou o .env estava errado).
    vencidos = expirar_pendentes(conexao)
    if vencidos:
        log.info("Desisti de %s aviso(s) pendente(s) com mais de %s min — o produto "
                 "ja deve ter vendido ou saido do ar.", vencidos, VALIDADE_PENDENTE_MIN)
    pendentes = buscar_pendentes(conexao)
    atrasados = len(pendentes) - len(novos)
    if atrasados > 0:
        log.info("Tambem ha %s item(ns) atrasado(s) de rodadas anteriores.", atrasados)

    erros = 0
    if pendentes:
        a_enviar = pendentes[:MAX_NOTIFICACOES_POR_RODADA]
        if len(pendentes) > MAX_NOTIFICACOES_POR_RODADA:
            log.warning(
                "Trava de seguranca: %s itens a avisar, mandando so os %s "
                "primeiros para nao virar spam. O resto ja esta salvo e sera "
                "avisado na proxima rodada.",
                len(pendentes), MAX_NOTIFICACOES_POR_RODADA,
            )

        a_enviar = aplicar_fotos(a_enviar, conexao)

        # ======================================================================
        # Envio em DUAS VELOCIDADES.
        #
        # O Discord nao tem o limite de ~1 msg/segundo que o Telegram tem.
        # Antes, os dois canais eram enviados juntos por item, numa fila so
        # com pausa — isso fazia o Discord esperar a MESMA fila lenta do
        # Telegram por nada: numa leva de 15 itens, o 15o chegava no Discord
        # uns 18s depois do 1o, sem motivo nenhum, so por estar atras na fila.
        #
        # Agora: Discord sai em RAJADA, todos ao mesmo tempo (paralelo).
        # Telegram continua em fila pausada, que e a unica coisa que
        # realmente precisa disso.
        # ======================================================================
        from concurrent.futures import ThreadPoolExecutor

        status_item = {
            item["id"]: {"tg_ok": item.get("_tg_ok", False),
                        "discord_ok": item.get("_discord_ok", False),
                        "canal_ok": item.get("_canal_ok", False)}
            for item in a_enviar
        }

        def _disparar_discord(item):
            webhook_canal = config.get("canais", {}).get(item.get("grupo", ""))
            tem_geral = bool(config["discord_webhook"])
            discord_ok = status_item[item["id"]]["discord_ok"]
            canal_ok = status_item[item["id"]]["canal_ok"]

            if tem_geral and not discord_ok:
                discord_ok = enviar_discord(item, config["discord_webhook"])
                if not discord_ok:
                    log.warning("Discord (canal geral) NAO recebeu (vai tentar de novo): %s",
                               item["titulo"][:50])
            if webhook_canal and not canal_ok:
                canal_ok = enviar_discord(item, webhook_canal)
                if not canal_ok:
                    log.warning("Discord/%s NAO recebeu (vai tentar de novo): %s",
                               NOME_GRUPO.get(item.get("grupo", ""), item.get("grupo")),
                               item["titulo"][:50])
            return item["id"], discord_ok or not tem_geral, canal_ok or not webhook_canal

        if a_enviar:
            log.info("Discord: disparando %s item(ns) em paralelo (sem fila).", len(a_enviar))
            with ThreadPoolExecutor(max_workers=min(len(a_enviar), 10)) as executor:
                for item_id, discord_ok, canal_ok in executor.map(_disparar_discord, a_enviar):
                    status_item[item_id]["discord_ok"] = discord_ok
                    status_item[item_id]["canal_ok"] = canal_ok

        # Telegram: fila sequencial pausada, unica que de fato precisa disso.
        # Reconfere o estoque RENTE a cada envio — como esta fila pode levar
        # ate ~18s numa leva grande, e tempo de sobra para um produto de 1
        # unidade (98% deles) esgotar entre a conferencia em lote (acima) e
        # a vez de cada item aqui.
        tem_telegram = bool(config["telegram_token"] and config["telegram_chat_id"])
        fila_telegram = [item for item in a_enviar
                         if tem_telegram and not status_item[item["id"]]["tg_ok"]]

        esgotaram_na_fila = 0
        falhas_seguidas = 0
        if fila_telegram and time.time() < _telegram_pausa["ate"]:
            log.warning("Telegram em pausa (falhou ha pouco): %s item(ns) esperam, "
                        "o Discord segue normal.", len(fila_telegram))
            fila_telegram = []
        for numero, item in enumerate(fila_telegram, 1):
            if falhas_seguidas >= 2:
                # O Telegram esta fora do ar ou lento. Cada tentativa custa ~30s;
                # insistir aqui pararia a leitura do site e atrasaria TODOS os
                # clientes (inclusive os do Discord). Desiste da fila desta
                # rodada; os itens ficam pendentes (validos por alguns minutos).
                _telegram_pausa["ate"] = time.time() + TELEGRAM_PAUSA_SEG
                log.error("TELEGRAM FALHOU %s vezes seguidas — pausando o Telegram por %ss "
                          "para nao atrasar o resto. %s item(ns) ficam pendentes.",
                          falhas_seguidas, TELEGRAM_PAUSA_SEG, len(fila_telegram) - numero + 1)
                break
            disponivel = ainda_disponivel(item["id"])
            if disponivel is False:
                esgotaram_na_fila += 1
                status_item[item["id"]]["tg_ok"] = True   # nada a reenviar aqui
                log.info("Esgotou na fila do Telegram, nao avisado: %s", item["titulo"][:60])
            else:
                log.info("Avisando no Telegram %s/%s: %s",
                         numero, len(fila_telegram), titulo_visivel(item)[:60])
                ok = enviar_telegram(item, config["telegram_token"], config["telegram_chat_id"])
                status_item[item["id"]]["tg_ok"] = ok
                falhas_seguidas = 0 if ok else falhas_seguidas + 1
                if not ok:
                    log.warning("Telegram NAO recebeu (vai tentar de novo na proxima rodada): %s",
                               item["titulo"][:50])
            # pausa para nao estourar o limite do Telegram (~1 msg/segundo)
            if numero < len(fila_telegram):
                time.sleep(DELAY_ENTRE_MENSAGENS)

        if not tem_telegram:
            for item in a_enviar:
                status_item[item["id"]]["tg_ok"] = True

        for item in a_enviar:
            completo = salvar_status_canais(conexao, item["id"], status_item[item["id"]])
            if not completo:
                erros += 1

        if esgotaram_na_fila:
            log.info("%s item(ns) esgotaram DENTRO da fila do Telegram (entre a "
                     "conferencia em lote e a vez de cada um).", esgotaram_na_fila)

    conexao.close()

    duracao = time.time() - inicio
    log.info(
        "Rodada concluida em %.1fs | lidos: %s | lancamentos novos: %s | "
        "erros de envio: %s", duracao, len(itens), len(novos), erros,
    )


# Os ids dos produtos guardam o momento de criacao (conferido contra a data
# das fotos em 791 de 796 produtos). Epoch: 01/01/2025 UTC.
# Descoberto em 07/10/2026: o relogio embutido nos ids esta 3h ADIANTADO em
# relacao ao UTC real (item criado ha ~15 min decodificava como 2h46 no
# futuro). O epoch efetivo e 31/12/2024 21:00 UTC.
EPOCH_ID = datetime(2024, 12, 31, 21, 0, tzinfo=timezone.utc)

# RECUPERACAO UNICA com prazo de validade, pedida em 07/10/2026: o bot ficou
# cego (Cloudflare) e os itens publicados no dia (08:05 a 09:21, horario de
# Brasilia) nao foram avisados. Quem inicia o bot ANTES deste instante
# recupera os itens criados nas ultimas RECUPERACAO_UNICA_HORAS horas (os
# que ainda tiverem estoque). Depois do prazo vira 0 sozinho — assim um
# reinicio futuro nao reanuncia itens ja avisados. Pode ser apagado depois.
RECUPERACAO_UNICA_ATE = datetime(2026, 10, 7, 13, 28, tzinfo=timezone.utc)
RECUPERACAO_UNICA_HORAS = 3

# ENVIO MANUAL UNICO, pedido em 09/10/2026: na primeira rodada, anuncia os N
# produtos MAIS NOVOS que ainda tenham estoque (conferido um a um no site).
# Vale so para quem iniciar o bot ate o instante abaixo — depois vira 0 sozinho,
# entao um reinicio futuro nao reposta nada. Para repetir sem mexer no codigo,
# use a variavel ENVIAR_ULTIMOS=5 no Railway (e apague depois).
ENVIO_MANUAL_ATE = datetime(2026, 10, 9, 5, 3, tzinfo=timezone.utc)
ENVIO_MANUAL_QTD = 5

# SEGUNDO ENVIO MANUAL (09/10/2026, ~07:15 de Brasilia): produtos que apareceram
# no site depois das 01:39 e nao foram avisados (113, comparados com o catalogo
# daquela hora). Entram na fila na primeira rodada de quem iniciar o bot ate o
# instante abaixo; o estoque e conferido de novo item a item antes de cada envio.
# Ficam atras de qualquer lancamento novo na fila e valem por mais tempo que o
# normal. Depois do prazo esta lista nao faz nada — pode ser apagada.
ENVIO_MANUAL_IDS_ATE = datetime(2026, 10, 9, 10, 45, tzinfo=timezone.utc)
ENVIO_MANUAL_IDS = ()   # lista de 09/10 ja enviada; esvaziada para um reinicio nao repostar

# Pausa do Telegram depois de falhas seguidas (ver rodar_coleta)
_telegram_pausa = {"ate": 0.0}

# Momento da ultima varredura profunda (0 = nunca fez)
_ultima_varredura = 0.0


def paginas_desta_rodada(primeira_vez: bool, segundos: int = VARREDURA_PADRAO_SEG) -> tuple:
    """
    Decide se esta rodada le so o topo ou faz a varredura profunda.

    Devolve (quantas_paginas, e_varredura_profunda).

    Na primeira vez SEMPRE varre fundo: e preciso semear a janela inteira,
    senao a primeira varredura acusaria centenas de produtos "novos" que
    na verdade ja existiam.
    """
    global _ultima_varredura

    agora = time.time()
    passou = (agora - _ultima_varredura) >= segundos

    if primeira_vez or passou:
        _ultima_varredura = agora
        return PAGINAS_PROFUNDAS, True

    return PAGINAS_RAPIDAS, False


def executar_rodada(config: dict) -> None:
    """Escolhe o modo certo: arquivo de texto (hospedado) ou banco (local)."""
    if config["arquivo_estado"]:
        rodar_coleta_arquivo(config)
    else:
        rodar_coleta(config)


def rodar_coleta_arquivo(config: dict) -> None:
    """
    Rodada no modo hospedado (GitHub Actions).

    Diferenca para o modo local: nao existe banco de dados. A memoria do
    que ja foi avisado e um arquivo de texto com um id por linha, que o
    proprio GitHub versiona entre uma rodada e outra.

    Outra diferenca importante: o id so entra na memoria DEPOIS que a
    mensagem foi enviada com sucesso. Se o envio falhar, o produto continua
    'nao avisado' e entra de novo na proxima rodada — nada se perde.
    """
    inicio = time.time()
    caminho = config["arquivo_estado"]
    log.info("=" * 60)
    log.info("Procurando lancamentos novos em %s", SITE_BASE)

    if not robots_permite(API_PRODUTOS):
        return

    ja_vistos = carregar_estado(caminho)
    conjunto = set(ja_vistos)
    primeira_vez = not ja_vistos

    espera = config["pico_segundos"] if em_horario_de_pico(config) else config["varredura_seg"]
    paginas, profunda = paginas_desta_rodada(primeira_vez, espera)
    if profunda:
        log.info("Varredura PROFUNDA: lendo %s paginas (~%s produtos).",
                 paginas, paginas * TAMANHO_PAGINA)

    registros = buscar_lancamentos(criar_sessao(), config["categoria"], paginas)
    if registros is None:
        log.error("Rodada abortada: nao consegui falar com o site.")
        return

    itens = extrair_itens(registros)
    log.info("Produtos lidos nesta rodada: %s", len(itens))
    if not itens:
        log.warning("Nenhum produto veio na resposta.")
        return

    # Primeira vez: so registra a base, sem encher voce de mensagens
    if primeira_vez:
        salvar_estado(caminho, [i["id"] for i in itens])
        log.info(
            "PRIMEIRA RODADA: guardei %s produtos como ponto de partida, "
            "sem enviar mensagem.", len(itens),
        )
        log.info("A partir de agora voce so sera avisado do que for LANCADO depois.")
        return

    # A API devolve do mais novo para o mais antigo; invertemos para avisar
    # na ordem em que os produtos foram publicados.
    novos = [i for i in itens if i["id"] not in conjunto][::-1]
    log.info("LANCAMENTOS NOVOS nesta rodada: %s", len(novos))

    if novos and len(novos) == len(itens):
        log.warning(
            "Todos os %s produtos lidos eram novos — pode ter escapado algum. "
            "Se repetir, aumente TAMANHO_PAGINA ou diminua o intervalo.", len(itens),
        )

    if not novos:
        log.info("Rodada concluida em %.1fs | nada novo.", time.time() - inicio)
        return

    a_enviar = novos[:MAX_NOTIFICACOES_POR_RODADA]
    if len(novos) > MAX_NOTIFICACOES_POR_RODADA:
        log.warning(
            "Trava de seguranca: %s novos, avisando so os %s primeiros. "
            "O resto entra na proxima rodada.",
            len(novos), MAX_NOTIFICACOES_POR_RODADA,
        )

    # Traduz so os que serao enviados agora
    if config["traduzir"]:
        log.info("Traduzindo %s titulo(s) para portugues...", len(a_enviar))
        for item in a_enviar:
            item["titulo_pt"] = traduzir(item["titulo"], None, config["traducao_email"])

    a_enviar = aplicar_fotos(a_enviar)

    erros = 0
    for numero, item in enumerate(a_enviar, 1):
        log.info("Avisando %s/%s: %s", numero, len(a_enviar), titulo_visivel(item)[:60])
        if notificar(item, config):
            ja_vistos.append(item["id"])
            salvar_estado(caminho, ja_vistos)   # grava a cada envio (incremental)
        else:
            erros += 1
        if numero < len(a_enviar):
            time.sleep(DELAY_ENTRE_MENSAGENS)

    log.info(
        "Rodada concluida em %.1fs | lidos: %s | novos: %s | erros de envio: %s",
        time.time() - inicio, len(itens), len(novos), erros,
    )


def testar_notificacao(config: dict) -> bool:
    """Manda uma mensagem de teste para conferir se a configuracao esta certa."""
    log.info("Enviando mensagem de TESTE...")
    item_teste = {
        "id": "teste",
        "titulo": "Teste do bot — se voce esta lendo isso, funcionou!",
        "titulo_pt": "",
        "tamanho": "",
        "compra": "",
        "grupo": "OUTROS",
        "imagem": "",
        "link": "",
        "preco": "",
        "categoria": "",
        "plataforma": "",
        "origem": "",
    }
    if notificar(item_teste, config):
        log.info("SUCESSO! Va conferir no seu grupo — a mensagem chegou.")
        return True

    log.error(
        "Nao consegui enviar. Confira o arquivo .env e as mensagens de "
        "erro acima. O README.md explica como pegar o token e o chat id."
    )
    return False


# ==========================================================================
#  8.4 BUSCAR UM PRODUTO E RECUPERAR AS FOTOS
# ==========================================================================
#  As imagens do CSSDeals continuam no ar mesmo depois do anuncio sair
#  do catalogo — some a listagem, nao os arquivos. Entao, se der para
#  achar o produto, da para recuperar as fotos.
#
#  A busca so enxerga o que ainda esta no catalogo. Produto ja retirado
#  nao aparece — para esses, so serve o endereco salvo antes.
# ==========================================================================

def buscar_por_titulo(termo: str, quantos: int = 10) -> list:
    """Procura produtos cujo titulo contenha o termo."""
    parametros = {
        "fields": 1, "categoryId": "", "page": 1, "pageSize": quantos,
        "priceMin": "0.00", "priceMax": "99999.00", "title": termo,
    }
    try:
        resposta = requests.get(API_PRODUTOS, params=parametros,
                                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resposta.raise_for_status()
        corpo = resposta.json()
    except Exception as erro:
        log.error("Busca falhou: %s", erro)
        return []
    if corpo.get("code") != 0:
        log.error("A API recusou a busca: %s", corpo.get("msg"))
        return []
    return (corpo.get("data") or {}).get("records") or []


def comando_buscar(termo: str) -> None:
    """Mostra os produtos encontrados com TODAS as fotos de cada um."""
    print()
    print("=" * 66)
    print("  BUSCA: {}".format(termo))
    print("=" * 66)

    achados = buscar_por_titulo(termo)
    if not achados:
        print()
        print("  Nenhum produto encontrado com esse texto no titulo.")
        print()
        print("  A busca so enxerga o catalogo atual. Se o anuncio ja saiu")
        print("  do ar, ele nao aparece aqui — nesse caso as fotos so podem")
        print("  ser recuperadas por um endereco salvo antes.")
        print()
        print("  Tente um trecho menor do titulo, ou uma palavra so.")
        return

    for numero, registro in enumerate(achados, 1):
        detalhe = buscar_detalhe(registro["id"])
        if detalhe is None or detalhe is REMOVIDO:
            detalhe = {}
        fotos = [f.get("url") for f in (detalhe.get("images") or []) if f.get("url")]
        sku = (detalhe.get("skus") or registro.get("skus") or [{}])[0]

        print()
        print("  {}. {}".format(numero, (registro.get("title") or "")[:60]))
        print("     CN¥ {}   tamanho {}   estoque {}".format(
            sku.get("price"), sku.get("size") or "-", sku.get("quantity")))
        print("     {}".format(URL_PRODUTO.format(id=registro["id"])))
        if fotos:
            print("     FOTOS ({}):".format(len(fotos)))
            for foto in fotos:
                print("       {}".format(foto))
        else:
            print("     (sem fotos no catalogo)")

    print()
    print("=" * 66)
    print("  As imagens continuam acessiveis mesmo se o anuncio sair do ar.")
    print("  Salve os enderecos acima se quiser guardar.")
    print("=" * 66)


# ==========================================================================
#  8.5 ASSISTENTE DE CONFIGURACAO  (bot.py --configurar)
# ==========================================================================
#  Faz as perguntas no Terminal e escreve o .env sozinho, para voce nao
#  precisar editar arquivo nenhum na mao.
# ==========================================================================

def _perguntar(rotulo: str) -> str:
    """Pergunta algo no Terminal e devolve a resposta sem espacos sobrando."""
    try:
        return input(rotulo).strip()
    except EOFError:
        return ""


def _gravar_env(valores: dict) -> None:
    """Escreve o arquivo .env e deixa ele legivel so por voce."""
    linhas = [
        "# Gerado pelo assistente (python bot.py --configurar)",
        "# NAO compartilhe este arquivo com ninguem.",
        "",
    ]
    for chave, valor in valores.items():
        linhas.append("{}={}".format(chave, valor))

    with open(".env", "w", encoding="utf-8") as arquivo:
        arquivo.write("\n".join(linhas) + "\n")

    try:
        os.chmod(".env", 0o600)   # so o seu usuario pode ler
    except OSError:
        pass


def _descobrir_chat_id(token: str) -> Optional[str]:
    """
    Descobre sozinho o ID do grupo do Telegram.

    Le as ultimas mensagens que o bot recebeu e pega o grupo de onde elas
    vieram. Por isso e preciso mandar uma mensagem no grupo antes.
    """
    try:
        resposta = requests.get(
            "https://api.telegram.org/bot{}/getUpdates".format(token), timeout=TIMEOUT
        )
        corpo = resposta.json()
    except Exception as erro:
        print("   Nao consegui falar com o Telegram: {}".format(erro))
        return None

    if not corpo.get("ok"):
        print("   O Telegram recusou o token: {}".format(corpo.get("description")))
        return None

    # Procura de tras para frente: o grupo mais recente primeiro
    for atualizacao in reversed(corpo.get("result") or []):
        for campo in ("message", "channel_post", "my_chat_member"):
            chat = (atualizacao.get(campo) or {}).get("chat") or {}
            if chat.get("type") in ("group", "supergroup", "channel"):
                print("   Grupo encontrado: {}".format(chat.get("title") or chat.get("id")))
                return str(chat.get("id"))
    return None


def _gh(*argumentos) -> tuple:
    """Roda um comando do GitHub CLI e devolve (deu_certo, saida)."""
    import subprocess
    caminho = os.path.expanduser("~/.local/bin/gh")
    if not os.path.exists(caminho):
        caminho = "gh"
    try:
        r = subprocess.run(
            [caminho] + list(argumentos),
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return False, "GitHub CLI (gh) nao encontrado."
    except Exception as erro:
        return False, str(erro)


def diagnosticar_telegram() -> None:
    """
    Investiga por que o grupo nao foi encontrado e diz exatamente o que fazer.

    Verifica, em ordem: se o token e valido, se ha um webhook atrapalhando,
    se o modo privacidade esta ligado e o que o bot realmente recebeu.
    """
    print()
    print("=" * 64)
    print("  DIAGNOSTICO DO TELEGRAM")
    print("=" * 64)

    token = _perguntar("\n  Cole o token do @BotFather: ")
    if ":" not in token or len(token) < 20:
        print("  Isso nao parece um token do Telegram.")
        return

    base = "https://api.telegram.org/bot{}".format(token)

    def chamar(metodo):
        try:
            return requests.get("{}/{}".format(base, metodo), timeout=TIMEOUT).json()
        except Exception as erro:
            return {"ok": False, "description": str(erro)}

    # ---- 1. o token funciona? ----
    print()
    print("  [1] Conferindo o token...")
    eu = chamar("getMe")
    if not eu.get("ok"):
        print("      X TOKEN INVALIDO: {}".format(eu.get("description")))
        print("      Copie o token de novo do @BotFather (inteiro, sem espacos).")
        return

    dados = eu["result"]
    print("      OK - bot: @{} ({})".format(dados.get("username"), dados.get("first_name")))
    print("      pode ser adicionado a grupos: {}".format(
        "sim" if dados.get("can_join_groups") else "NAO"))
    print("      le mensagens comuns de grupo: {}".format(
        "sim" if dados.get("can_read_all_group_messages") else "NAO (modo privacidade LIGADO)"))

    # ---- 2. tem webhook atrapalhando? ----
    print()
    print("  [2] Conferindo se ha webhook configurado...")
    webhook = chamar("getWebhookInfo")
    url_webhook = (webhook.get("result") or {}).get("url") or ""
    if url_webhook:
        # ATENCAO: webhook NAO impede o envio de alertas. Ele so impede
        # LER mensagens (getUpdates). Se outro servico registrou esse
        # webhook (AccessManager, gateway de pagamento, etc), apaga-lo
        # QUEBRARIA aquele servico. Por isso nao mandamos apagar.
        info = webhook.get("result") or {}
        print("      Existe um webhook ativo: {}...".format(url_webhook[:45]))
        print()

        # --- saude da entrega: diz se o servico dono do webhook responde ---
        pendentes = info.get("pending_update_count", 0)
        erro_msg = info.get("last_error_message")
        erro_data = info.get("last_error_date")

        print("      SAUDE DA ENTREGA (util se o bot nao responde):")
        print("        mensagens na fila esperando: {}".format(pendentes))

        if erro_msg:
            quando = ""
            if erro_data:
                quando = datetime.fromtimestamp(erro_data).strftime(" em %d/%m %H:%M")
            print("        ULTIMO ERRO{}: {}".format(quando, str(erro_msg)[:70]))
            print()
            print("        >> O Telegram esta tentando entregar e FALHANDO.")
            print("           O servico dono do webhook esta fora do ar ou")
            print("           recusando. Fale com o suporte dele.")
        elif pendentes > 5:
            print()
            print("        >> Sem erro de entrega, mas {} mensagens empilhadas.".format(pendentes))
            print("           O servico recebe mas nao esta processando —")
            print("           provavelmente falta configura-lo no painel dele")
            print("           (mensagem de boas-vindas, plano, pagamento).")
        else:
            print("        nenhum erro de entrega registrado")
            print()
            print("        >> O Telegram ENTREGA normalmente ao servico.")
            print("           Se o bot nao responde, o problema esta na")
            print("           configuracao dentro do painel do servico,")
            print("           nao na conexao nem no seu bot de alertas.")
        print()
        print("      Isso NAO atrapalha os alertas — o bot so precisa ENVIAR,")
        print("      e enviar continua funcionando normalmente.")
        print()
        print("      NAO APAGUE esse webhook: ele provavelmente pertence a")
        print("      outro servico ligado ao seu bot (gateway de pagamento,")
        print("      controle de assinatura). Apagar quebraria aquele servico.")
        print()
        print("      So nao da para descobrir o ID do grupo automaticamente")
        print("      enquanto ele existir. Vou pedir o ID e testar o envio.")
        print()

        chat_id = _perguntar("      Cole o TELEGRAM_CHAT_ID (o mesmo do Railway): ")
        if not chat_id:
            print("      Sem o ID nao da para testar. Ele esta nas Variables do Railway.")
            return

        print()
        print("  [3] Mandando uma mensagem de TESTE...")
        print()
        ok = enviar_telegram(
            {"titulo": "Teste do bot — se voce esta lendo isso, o envio funciona!",
             "titulo_pt": "", "imagem": "", "link": "", "preco": "",
             "categoria": "", "plataforma": ""},
            token, chat_id,
        )
        print()
        print("=" * 64)
        if ok:
            print("  ENVIADO! Token, ID e permissao estao OK.")
            print("  Se a mensagem chegou, os alertas vao chegar tambem.")
        else:
            print("  FALHOU. Veja o erro acima.")
            print("  Causa mais comum: o bot deixou de ser administrador do")
            print("  grupo (remover e readicionar zera as permissoes dele).")
        print("=" * 64)
        return

    print("      OK - nenhum webhook configurado")

    # ---- 3. o que o bot recebeu? ----
    print()
    print("  [3] Vendo o que o bot recebeu...")
    atualizacoes = chamar("getUpdates")
    if not atualizacoes.get("ok"):
        print("      X {}".format(atualizacoes.get("description")))
        return

    lista = atualizacoes.get("result") or []
    print("      {} evento(s) recebido(s)".format(len(lista)))

    grupos = {}
    for item in lista:
        for campo, conteudo in item.items():
            if not isinstance(conteudo, dict):
                continue
            chat = conteudo.get("chat") or {}
            if chat.get("id"):
                print("        - {}: chat '{}' (tipo {})".format(
                    campo, chat.get("title") or chat.get("first_name"), chat.get("type")))
                if chat.get("type") in ("group", "supergroup", "channel"):
                    grupos[str(chat["id"])] = chat.get("title")

    # ---- veredito ----
    print()
    print("=" * 64)
    if grupos:
        print("  ENCONTRADO!")
        for ident, titulo in grupos.items():
            print("     {}  ->  {}".format(titulo, ident))

        # ---- teste de envio de verdade ----
        print()
        print("  [4] Mandando uma mensagem de TESTE agora...")
        print()
        algum_ok = False
        for ident, titulo in grupos.items():
            ok = enviar_telegram(
                {"titulo": "Teste do bot — se voce esta lendo isso, "
                           "o envio funciona!",
                 "titulo_pt": "", "imagem": "", "link": "", "preco": "",
                 "categoria": "", "plataforma": ""},
                token, ident,
            )
            if ok:
                algum_ok = True
                print("      ENVIADO para '{}' — va conferir!".format(titulo))
            else:
                print("      FALHOU em '{}' (veja o erro acima).".format(titulo))
                print("      Causa mais comum: o grupo/canal so deixa")
                print("      administradores escreverem e o bot nao e admin.")

        print()
        print("=" * 64)
        if algum_ok:
            print("  A MENSAGEM CHEGOU? Entao token, ID e permissao estao OK.")
            print()
            print("  Use estes valores no Railway (aba Variables):")
            for ident in grupos:
                print("     TELEGRAM_CHAT_ID = {}".format(ident))
        else:
            print("  NAO CONSEGUI ENVIAR. O bot nao vai funcionar assim,")
            print("  nem no Railway. Corrija a permissao e rode de novo.")
        return
    else:
        print("  NENHUM GRUPO ENCONTRADO. O que fazer:")
        print()
        if not dados.get("can_read_all_group_messages"):
            print("  CAUSA MAIS PROVAVEL: modo privacidade ligado.")
            print("  Bots do @BotFather nascem sem poder ler mensagens comuns")
            print("  de grupo — so mensagens que comecam com barra ( / ).")
            print()
            print("  SOLUCAO RAPIDA (10 segundos):")
            print("     Mande  /start  no grupo. Mensagens com barra sempre")
            print("     chegam ao bot, mesmo com o modo privacidade ligado.")
            print()
            print("  SOLUCAO DEFINITIVA (se preferir):")
            print("     No @BotFather mande /setprivacy, escolha @{}".format(
                dados.get("username")))
            print("     e clique em Disable. Depois REMOVA e ADICIONE o bot")
            print("     no grupo de novo (a mudanca so vale ao reentrar).")
        else:
            print("  O modo privacidade ja esta desligado, entao confira:")
            print("     - o bot @{} esta MESMO nesse grupo?".format(dados.get("username")))
            print("     - voce mandou a mensagem DEPOIS de adicionar ele?")
            print("     - o token e desse bot mesmo (nao de outro que voce criou)?")
        print()
        print("  Depois disso, rode este diagnostico de novo.")
    print("=" * 64)


def configurar_github() -> None:
    """
    Guarda o token do Telegram (ou o webhook do Discord) no cofre do GitHub.

    Tudo acontece na SUA maquina: voce digita o token aqui, ele vai direto
    para o cofre do GitHub e nao fica salvo em arquivo nenhum.
    """
    print()
    print("=" * 64)
    print("  GUARDAR AS SENHAS NO COFRE DO GITHUB")
    print("=" * 64)

    ok, saida = _gh("auth", "status")
    if not ok:
        print()
        print("  Voce ainda nao autorizou o GitHub CLI.")
        print("  Rode primeiro:  ~/.local/bin/gh auth login --web")
        return

    repo = _perguntar("\n  Qual o repositorio? (ex: seu-usuario/bot-cssdeals): ")
    if "/" not in repo:
        print("  Formato invalido. Precisa ser usuario/repositorio.")
        return

    print()
    print("  Onde voce quer receber os avisos?")
    print("    1 - Telegram")
    print("    2 - Discord")
    escolha = _perguntar("  Digite 1 ou 2: ")

    # ------------------------------- TELEGRAM ------------------------------
    if escolha == "1":
        print()
        print("  Passo 1: pegue o token com o @BotFather no Telegram")
        print("           (mande /newbot e siga as perguntas)")
        print()
        token = _perguntar("  Cole o token aqui: ")
        if ":" not in token or len(token) < 20:
            print("  Isso nao parece um token do Telegram.")
            return

        print()
        print("  Passo 2: antes de continuar, confirme que voce ja:")
        print("     a) criou o grupo no Telegram")
        print("     b) adicionou o SEU bot nesse grupo")
        print("     c) mandou qualquer mensagem no grupo (ex: 'oi')")
        print()
        _perguntar("  Feito? Aperte Enter para eu procurar o grupo... ")

        chat_id = _descobrir_chat_id(token)
        if not chat_id:
            print()
            print("  Nao achei nenhum grupo.")
            print("  O mais comum: faltou mandar uma mensagem no grupo DEPOIS")
            print("  de adicionar o bot. Faca isso e rode este comando de novo.")
            return

        print()
        print("  Enviando uma mensagem de teste antes de salvar...")
        if not enviar_telegram(
            {"titulo": "Teste do bot — funcionou!", "titulo_pt": "", "imagem": "",
             "link": "", "preco": "", "categoria": "", "plataforma": ""},
            token, chat_id,
        ):
            print()
            print("  A mensagem de teste NAO chegou. Nao vou salvar nada.")
            print("  Confira se o bot esta mesmo no grupo e tente de novo.")
            return

        print("  Mensagem de teste enviada! Confira o grupo.")
        print()
        print("  Salvando no cofre do GitHub...")
        segredos = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}

    # ------------------------------- DISCORD -------------------------------
    elif escolha == "2":
        print()
        url = _perguntar("  Cole a URL do webhook do Discord: ")
        if "discord.com/api/webhooks/" not in url and \
           "discordapp.com/api/webhooks/" not in url:
            print("  Isso nao parece a URL de um webhook do Discord.")
            return

        print()
        print("  Enviando uma mensagem de teste antes de salvar...")
        if not enviar_discord(
            {"titulo": "Teste do bot — funcionou!", "titulo_pt": "", "imagem": "",
             "link": "", "preco": "", "categoria": "", "plataforma": ""}, url,
        ):
            print()
            print("  A mensagem de teste NAO chegou. Nao vou salvar nada.")
            return

        print("  Mensagem de teste enviada! Confira o canal.")
        print()
        print("  Salvando no cofre do GitHub...")
        segredos = {"DISCORD_WEBHOOK_URL": url}

    else:
        print("  Opcao invalida.")
        return

    # --------------------------- gravar no cofre ---------------------------
    for nome, valor in segredos.items():
        ok, saida = _gh("secret", "set", nome, "--repo", repo, "--body", valor)
        if ok:
            print("     {} ....... guardado".format(nome))
        else:
            print("     {} ....... FALHOU: {}".format(nome, saida[:120]))
            return

    print()
    print("=" * 64)
    print("  TUDO PRONTO!")
    print()
    print("  O bot ja esta no ar. Ele roda sozinho na nuvem do GitHub;")
    print("  seu computador pode ficar desligado.")
    print()
    print("  A primeira rodada guarda os produtos atuais sem avisar.")
    print("  Da segunda em diante chegam so os lancamentos novos.")
    print("=" * 64)


def assistente_configuracao() -> None:
    """Conversa com voce no Terminal e monta o .env do zero."""
    print()
    print("=" * 64)
    print("  ASSISTENTE DE CONFIGURACAO")
    print("=" * 64)

    if os.path.exists(".env"):
        if _perguntar("Ja existe um .env. Quer refazer? (s/n): ").lower() not in ("s", "sim"):
            print("Nada foi alterado.")
            return

    print()
    print("  Onde voce quer receber os avisos?")
    print("    1 - Discord   (mais facil: 4 cliques, sem criar bot)")
    print("    2 - Telegram")
    escolha = _perguntar("  Digite 1 ou 2: ")

    valores = {"TRADUZIR": "sim", "TRADUCAO_EMAIL": "", "CATEGORIA_ID": ""}

    # ------------------------------- DISCORD -------------------------------
    if escolha == "1":
        print()
        print("  Como pegar a URL (no app do Discord):")
        print("    1. Passe o mouse no canal que vai receber os avisos")
        print("    2. Clique na engrenagem (Editar Canal)")
        print("    3. Menu da esquerda: Integracoes -> Webhooks -> Novo webhook")
        print("    4. Clique em 'Copiar URL do Webhook'")
        print()

        url = _perguntar("  Cole a URL aqui e aperte Enter: ")
        if not url.startswith("https://discord.com/api/webhooks/") and \
           not url.startswith("https://discordapp.com/api/webhooks/"):
            print()
            print("  Isso nao parece a URL de um webhook do Discord.")
            print("  Ela precisa comecar com https://discord.com/api/webhooks/")
            return

        valores["DISCORD_WEBHOOK_URL"] = url
        valores["TELEGRAM_BOT_TOKEN"] = ""
        valores["TELEGRAM_CHAT_ID"] = ""

    # ------------------------------- TELEGRAM ------------------------------
    elif escolha == "2":
        print()
        print("  Como pegar o token:")
        print("    1. No Telegram, procure @BotFather (tem selo azul)")
        print("    2. Mande /newbot e siga as perguntas")
        print("    3. Ele responde com o token (algo como 123456:AAH...)")
        print()

        token = _perguntar("  Cole o token aqui e aperte Enter: ")
        if ":" not in token or len(token) < 20:
            print()
            print("  Isso nao parece um token do Telegram.")
            return

        print()
        print("  Agora preciso descobrir o ID do grupo. Antes de continuar:")
        print("    1. Crie o grupo (se ainda nao criou)")
        print("    2. Adicione o seu bot ao grupo")
        print("    3. Mande QUALQUER mensagem no grupo (ex: 'oi')")
        print()
        _perguntar("  Feito isso, aperte Enter para eu procurar... ")

        chat_id = _descobrir_chat_id(token)
        if not chat_id:
            print()
            print("  Nao achei nenhum grupo.")
            print("  Confirme que o bot foi adicionado E que voce mandou uma")
            print("  mensagem no grupo depois disso. Depois rode de novo:")
            print("      .venv/bin/python bot.py --configurar")
            return

        valores["TELEGRAM_BOT_TOKEN"] = token
        valores["TELEGRAM_CHAT_ID"] = chat_id
        valores["DISCORD_WEBHOOK_URL"] = ""

    else:
        print("  Opcao invalida. Rode de novo e digite 1 ou 2.")
        return

    # ------------------------- gravar e testar -----------------------------
    _gravar_env(valores)
    print()
    print("  Arquivo .env gravado. Mandando uma mensagem de teste...")
    print()

    load_dotenv(override=True)
    config = {
        "telegram_token": valores.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": valores.get("TELEGRAM_CHAT_ID", ""),
        "discord_webhook": valores.get("DISCORD_WEBHOOK_URL", ""),
    }
    deu_certo = testar_notificacao(config)

    print()
    print("=" * 64)
    if deu_certo:
        print("  TUDO PRONTO! A mensagem de teste chegou no seu grupo.")
        print()
        print("  O agendador ja esta ligado e roda a cada 15 minutos.")
        print("  A primeira rodada guarda os produtos atuais sem avisar;")
        print("  depois disso voce recebe so os lancamentos novos.")
    else:
        print("  A CONFIGURACAO FOI SALVA, MAS O TESTE NAO PASSOU.")
        print()
        print("  Veja a mensagem de erro logo acima — ela diz o motivo.")
        print("  O mais comum e a URL/token ter sido copiado pela metade.")
        print()
        print("  Para tentar de novo:")
        print("      .venv/bin/python bot.py --configurar")
    print("=" * 64)


# ==========================================================================
#  9. PONTO DE ENTRADA
# ==========================================================================

def testar_rede() -> None:
    """
    Na partida, testa se ESTE servidor alcanca cada servico, por IPv4 e IPv6.

    Existe porque a mensagem de erro engana: quando o IPv4 esgota o tempo
    e o IPv6 nao tem rota, so aparece "Network is unreachable" (o ultimo
    erro), escondendo que o IPv4 tambem falhou.

    Se o IPv4 funciona e o IPv6 nao, forca todas as conexoes por IPv4.
    """
    import socket

    def alcanca(host, familia):
        try:
            enderecos = socket.getaddrinfo(host, 443, familia, socket.SOCK_STREAM)
        except socket.gaierror:
            return "sem endereco"
        motivo = "?"
        for af, tipo, proto, _, destino in enderecos[:2]:
            sock = socket.socket(af, tipo, proto)
            sock.settimeout(5)
            try:
                sock.connect(destino)
                return "OK"
            except socket.timeout:
                motivo = "sem resposta em 5s"
            except OSError as erro:
                motivo = erro.strerror or str(erro)
            finally:
                sock.close()
        return "FALHOU (" + motivo + ")"

    ipv4_telegram = "?"
    for host in ("api.telegram.org", "discord.com", "cssdeals.com"):
        v4 = alcanca(host, socket.AF_INET)
        v6 = alcanca(host, socket.AF_INET6)
        log.info("Rede ate %-16s IPv4: %-28s IPv6: %s", host, v4, v6)
        if host == "api.telegram.org":
            ipv4_telegram, ipv6_telegram = v4, v6

    if ipv4_telegram == "OK" and ipv6_telegram != "OK":
        import urllib3.util.connection as conexao_urllib3
        conexao_urllib3.allowed_gai_family = lambda: socket.AF_INET
        log.info("IPv6 indisponivel aqui — conexoes forcadas por IPv4.")
    elif ipv4_telegram != "OK" and ipv6_telegram != "OK":
        log.error("ESTE SERVIDOR NAO ALCANCA O TELEGRAM (nem IPv4 nem IPv6). "
                  "O problema e a rede do servidor, nao o bot.")


def main() -> None:
    leitor = argparse.ArgumentParser(
        description="Bot de coleta com notificacao no Telegram/Discord."
    )
    leitor.add_argument(
        "--loop", action="store_true",
        help="Roda sem parar (intervalo pelo INTERVALO_SEGUNDOS do .env).",
    )
    leitor.add_argument(
        "--configurar", action="store_true",
        help="Assistente que pergunta os dados e monta o .env sozinho.",
    )
    leitor.add_argument(
        "--configurar-github", dest="configurar_github", action="store_true",
        help="Guarda o token/webhook no cofre do GitHub (para rodar hospedado).",
    )
    leitor.add_argument(
        "--diagnosticar-telegram", dest="diag_telegram", action="store_true",
        help="Descobre por que o grupo do Telegram nao foi encontrado.",
    )
    leitor.add_argument(
        "--buscar", metavar="TEXTO",
        help="Procura um produto pelo titulo e mostra todas as fotos dele.",
    )
    leitor.add_argument(
        "--testar", action="store_true",
        help="So envia uma mensagem de teste e sai (nao coleta nada).",
    )
    argumentos = leitor.parse_args()

    # O assistente roda ANTES da checagem de configuracao — e justamente
    # ele que cria o .env que a checagem exige.
    if argumentos.buscar:
        comando_buscar(argumentos.buscar)
        return

    if argumentos.configurar:
        assistente_configuracao()
        return

    if argumentos.configurar_github:
        configurar_github()
        return

    if argumentos.diag_telegram:
        diagnosticar_telegram()
        return

    config = carregar_config()
    testar_rede()
    log.info("MEMORIA DO BOT em %s — %s.", os.path.abspath(BANCO_DADOS),
             "arquivo ja existe: continua de onde parou" if os.path.exists(BANCO_DADOS)
             else "arquivo NOVO (vazio): o bot comeca do zero")

    if argumentos.testar:
        if not testar_notificacao(config):
            sys.exit(1)
        return

    if argumentos.loop:
        log.info(
            "MODO CONTINUO ligado — verificando a cada %s segundos.",
            config["intervalo"],
        )
        while True:
            try:
                executar_rodada(config)
            except Exception as erro:
                # Blindagem final: nada derruba o loop
                log.exception("Erro inesperado na rodada: %s", erro)

            # O ciclo acompanha a cadencia da varredura, que e quem
            # de fato encontra os lancamentos.
            # (a pausa entre rodadas fica AQUI, depois dos avisos — antes ela
            # atrasava todo aviso em 1,5s sem proteger o site de nada)
            if em_horario_de_pico(config):
                time.sleep(config["pico_segundos"] + DELAY_ENTRE_REQUISICOES)
            else:
                time.sleep(min(config["intervalo"], config["varredura_seg"])
                           + DELAY_ENTRE_REQUISICOES)
    else:
        executar_rodada(config)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\nBot encerrado por voce. Ate mais!")
        sys.exit(0)
