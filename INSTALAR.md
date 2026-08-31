# Instalar este bot em outro lugar

Guia curto para levar o bot para qualquer serviço que rode Python.
O bot **só envia** mensagens — não precisa de porta, domínio nem banco externo.

---

## O que ele precisa

| | |
|---|---|
| Python | 3.9 ou mais novo |
| Dependências | `pip install -r requirements.txt` |
| Comando | `python bot.py --loop` |
| Porta / domínio | **nenhum** — não é site, é processo de fundo |
| Disco | opcional (veja "Memória" abaixo) |

---

## Variáveis de ambiente

**Obrigatórias — pelo menos um canal:**

| Variável | Para quê |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token do @BotFather |
| `TELEGRAM_CHAT_ID` | id do grupo/canal (começa com `-`) |
| `DISCORD_WEBHOOK_URL` | webhook do canal do Discord |

Preencha Telegram, Discord, ou os dois. Com os dois, envia para ambos.

**Opcionais — todas têm padrão:**

| Variável | Padrão | Para quê |
|---|---|---|
| `INTERVALO_SEGUNDOS` | `60` | pausa entre as leituras rápidas |
| `MINUTOS_ENTRE_VARREDURAS` | `2` | intervalo da varredura profunda — **define o atraso dos avisos** |
| `FOTO_DO_ANUNCIO` | `0` | qual foto usar (0 = primeira) |
| `TRADUZIR` | `nao` | traduzir títulos para português |
| `MOSTRAR_REAL` | `nao` | mostrar preço convertido em reais |
| `CATEGORIA_ID` | *(vazio)* | monitorar só uma aba do site |
| `ARQUIVO_ESTADO` | *(vazio)* | usar arquivo de texto no lugar do banco |

---

## Memória (importante)

O bot guarda o que já avisou para não repetir. Dois modos:

**Banco SQLite** *(padrão)* — cria `dados.db` na pasta. Se o serviço apagar
o disco a cada reinício, ele recomeça: faz uma rodada silenciosa guardando
os produtos atuais e só avisa o que vier depois. Você não recebe repetido;
só deixa de ser avisado do que saiu durante o reinício.

**Arquivo de texto** — defina `ARQUIVO_ESTADO=vistos.txt`. Útil onde o
estado precisa ser versionado (foi assim no GitHub Actions).

---

## Passo a passo genérico

```bash
git clone https://github.com/nicolasrsaantos-alt/bot-cssdeals.git
cd bot-cssdeals
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env      # preencha o .env
.venv/bin/python bot.py --configurar   # ou use o assistente

.venv/bin/python bot.py --loop
```

Em serviços de nuvem, use as variáveis de ambiente do painel em vez do `.env`.

---

## Comandos úteis

| Comando | O que faz |
|---|---|
| `python bot.py` | roda uma vez e sai |
| `python bot.py --loop` | roda continuamente *(use este em produção)* |
| `python bot.py --configurar` | assistente que monta o `.env` e testa o envio |
| `python bot.py --diagnosticar-telegram` | descobre problemas de token, webhook e grupo |
| `python bot.py --testar` | manda só uma mensagem de teste |

---

## Como saber se está funcionando

No log, logo ao subir:

```
Canais ativos: Telegram + Discord
MODO CONTINUO ligado — verificando a cada 60 segundos.
PRIMEIRA RODADA: guardei 1000 produtos como ponto de partida, sem enviar mensagem.
```

A primeira rodada é silenciosa de propósito. A partir da segunda, chegam
só os lançamentos novos.

---

## Onde o bot busca os dados

API interna do próprio cssdeals.com:

- `GET /api/product` — lista de produtos, ordenada por id (ordem de criação)
- `GET /api/product/{id}` — detalhe, é onde ficam todas as fotos
- `POST /api/cart {productId, quantity}` — devolve o link do carrinho do CSSBuy

O `robots.txt` do site libera acesso automatizado (`User-agent: * / Allow: /`).
O bot se identifica honestamente e confere o robots antes de cada rodada.
