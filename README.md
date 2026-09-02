# Bot de Coleta + Notificação

Bot que vigia o **cssdeals.com** e **te avisa no Telegram (ou Discord) toda
vez que um produto novo é lançado** — com título traduzido para português,
foto e link.

Roda **na nuvem do GitHub**, de graça. Seu computador pode ficar desligado.

Escrito para quem não é programador. É só seguir os passos na ordem.

---

## ✅ Como o bot encontra os lançamentos

O site tem dezenas de abas (Shoes, Hoodie, Pants...) com subabas de tamanho e
dezenas de páginas cada. Varrer tudo daria centenas de requisições por rodada.

Não é preciso: a API interna devolve os produtos **ordenados por ID**, e ID é
a ordem em que o produto foi **criado**.

### O detalhe que quase passou batido

Ordem de criação **não é** ordem de publicação. O cssdeals às vezes torna
visível um produto que foi criado dias atrás — ele carrega o ID antigo e
aparece **no meio da lista**, nunca no topo. É por isso que um tênis publicado
hoje pode cair na página 3 da aba Shoes.

Ler só o topo deixaria esses produtos **invisíveis para sempre**, porque eles
nunca sobem.

### A solução: duas velocidades

| Tipo de rodada | Frequência | Lê | Serve para |
|---|---|---|---|
| **Rasa** | a cada 60s | 200 produtos | pegar os recém-criados, rápido |
| **Profunda** | **a cada 45s** · 15s no pico | 1.000 produtos (~3 dias) | achar os que ficaram visíveis agora |

A varredura profunda procura **qualquer ID que o bot ainda não conheça**,
independentemente da posição na lista.

Comprovado em teste: um produto na posição 500 **não é** detectado pela rodada
rasa e **é** detectado pela profunda.

### Uma armadilha da API

Pedir `pageSize=200` devolve **20** produtos, não 200. Acima de 100 a API
ignora o pedido e volta ao mínimo, sem erro nenhum. O máximo real é **100**.

### O que chega no seu grupo

```
🆕 Unisex Chest Bag Multi-functional Mobile Phone Bag Waist Bag
Tamanho: L
Hat&Bags · Taobao
Preço: CN¥ 57,50
——————————————————
  🛒 COMPRE AQUI
——————————————————
Ver no CSSDeals
[foto do produto]
```

**"COMPRE AQUI" joga o produto direto no carrinho do CSSBuy.** O cliente
pula os passos de abrir o produto, clicar em Buy Now e Copy Link.

> **Como funciona:** o botão Buy Now do CSSDeals faz um
> `POST /api/cart {productId, quantity}` e recebe de volta um `redirectUrl`.
> Esse endereço depende só do id do produto no CSSDeals — que o bot já tem —,
> então ele é montado direto, sem requisição extra:
>
> `https://www.cssbuy.com/waiting?type=cssdeals&productId={id}&quantity=1`
>
> Verificado: em 5 de 5 produtos o link gerado é **idêntico** ao que a
> API do próprio site devolve.

**O tamanho aparece logo abaixo do nome.** Se o produto não tiver tamanho
cadastrado (serviços, kits, eletrônicos), a linha simplesmente não aparece —
em vez de mostrar um campo vazio.

**Preços em Yuan (CN¥)**, como o site publica.
**Títulos sem tradução**, exatamente como estão no anúncio.
**Foto: a primeira imagem do anúncio feito no CSSDeals.**

> **Por que a segunda foto:** a listagem da API devolve uma imagem só, e
> em boa parte dos produtos ela aponta para o site de origem (1688, Taobao,
> Weidian) em vez do CSSDeals. As fotos do anúncio do CSSDeals só existem
> no endpoint de detalhe — o bot busca lá e usa a segunda, que costuma
> mostrar melhor o produto que a capa.
>
> Se o produto tiver só uma foto, usa essa. Se a busca falhar, usa a da
> listagem — nunca deixa de avisar por causa de imagem.
>
> Para mudar qual foto, use a variável `FOTO_DO_ANUNCIO` no Railway
> (`0` = primeira, `1` = segunda, `2` = terceira...). Se o produto tiver
> menos fotos que o número pedido, usa a última disponível.

> Alguns títulos já vêm em português — isso é o próprio site publicando
> assim, não tradução do bot.

#### Se um dia quiser mudar

No Railway, aba **Variables**:

| Variável | Para quê |
|---|---|
| `MOSTRAR_REAL=sim` | Mostra também o valor convertido: `CN¥ 57,50 (~R$ 44,03)` |
| `TRADUZIR=sim` | Traduz os títulos para português |

A conversão usa cotação real do dia (AwesomeAPI, com ExchangeRate de
reserva) e é buscada uma vez por hora. Se as duas fontes falharem, o preço
aparece só em Yuan — nunca deixa de te avisar por causa disso.

### Primeira vez que rodar

Na **primeira rodada** o bot guarda os 50 produtos atuais como ponto de
partida e **não envia mensagem nenhuma**. Isso é de propósito — sem isso você
levaria 50 notificações de produtos que já existiam antes de você ligar o bot.

A partir da segunda rodada, você só recebe o que for **lançado depois disso**.

---

## ⚠️ Correção importante sobre o que eu te disse antes

Na minha primeira análise eu disse que o `cssdeals.com` era só uma
demonstração de template e que **nunca mudava**. **Eu estava errado**, e é
bom que estivesse.

O que aconteceu: as páginas HTML que aparecem quando você abre o site *são*
de um template de demonstração, com produtos falsos (camiseta de $120, etc).
Eu parei a análise nelas. Mas escavando o JavaScript encontrei a API real
por trás — e ela tem **~10.000 produtos de verdade, atualizados diariamente**.

**O bot usa a API real.** Aqueles produtos falsos não entram.

---

## Sobre o robots.txt e proteção anti-bot (você pediu pra eu conferir)

- ✅ **Coleta permitida.** A regra geral (`User-agent: *`) é `Allow: /` — vale
  também para a API. O bot ainda checa o robots.txt sozinho antes de cada
  rodada.
- ⚠️ O site bloqueia **robôs de treinar Inteligência Artificial** (GPTBot,
  ClaudeBot, CCBot...). Nosso bot não faz isso — só lê e te notifica.
- ✅ **Não tem captcha nem bloqueio anti-bot.** O site passa por Cloudflare,
  mas testei: acesso simples responde normal. **Não precisa de Playwright nem
  de contornar proteção nenhuma.**
- O bot se identifica honestamente como `BotColetaPessoal/1.0` em vez de
  fingir ser um navegador.
- **Nenhum dado pessoal é coletado** — só título, foto, preço e link.

---

## Onde o bot roda

**No Railway** — um servidor na nuvem, sempre ligado. Seu computador pode
ficar desligado.

O bot fica em **loop contínuo**: consulta o site a cada **60 segundos** e
avisa na hora. Não depende de agendador de terceiros.

### Por que saímos do GitHub Actions

Ficou 24 horas sem disparar **nenhuma** rodada agendada. Testamos tudo:
e-mail verificado, workflow `active`, branch correta, cron válido, Actions
habilitado. Até subimos um workflow de 10 linhas que só imprime a data —
e nem ele rodou.

Rodadas manuais sempre funcionaram, o que provou que o bot estava certo.
O problema era o agendamento do GitHub nessa conta (provavelmente restrição
de conta recém-criada, que eles não documentam).

O agendamento do GitHub ficou **desligado** para não enviar mensagem
duplicada. O disparo manual continua disponível como reserva:
*Actions → Monitor CSSDeals → Run workflow*.

---

## Como colocar no Railway (uma vez só, ~5 minutos)

### 1. Criar a conta

Acesse **railway.com** → **Login with GitHub**. Como o código já está no seu
GitHub, isso liga as duas contas automaticamente.

### 2. Criar o projeto

**New Project** → **Deploy from GitHub repo** → escolha **bot-cssdeals**

Se ele pedir permissão para acessar seus repositórios, autorize.

### 3. Configurar as senhas

No projeto, abra a aba **Variables** e adicione uma por uma
(**New Variable** → nome e valor):

| Nome | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | o token do @BotFather |
| `TELEGRAM_CHAT_ID` | o ID do grupo (começa com `-`) |
| `TRADUZIR` | `sim` |
| `INTERVALO_SEGUNDOS` | `60` |

> **Não lembra o token ou o ID?** Eles estão guardados no cofre do GitHub,
> mas de lá não dá para ler de volta (é assim de propósito). Pegue o token
> de novo com o @BotFather e rode `bot.py --diagnosticar-telegram` para
> redescobrir o ID do grupo.

### 4. Pronto

O Railway detecta o Python sozinho, instala o `requirements.txt` e roda
`python bot.py --loop` (está definido no `Procfile` e no `railway.json`).

Acompanhe pela aba **Deploy Logs**. Você deve ver:

```
MODO CONTINUO ligado — verificando a cada 60 segundos.
PRIMEIRA RODADA: guardei 50 produtos como ponto de partida, sem enviar mensagem.
```

A partir daí chegam só os lançamentos novos, em menos de 1 minuto.

### Se o bot reiniciar

Ao publicar uma atualização ou se o servidor reiniciar, a memória recomeça
e o bot faz uma nova "primeira rodada" — guarda os 50 atuais **sem avisar**.
Você não recebe mensagem repetida; no máximo deixa de ser avisado dos
produtos publicados durante o reinício (segundos).

---

## O que cada arquivo faz

| Arquivo | Para que serve |
|---|---|
| `bot.py` | O programa. Todo comentado em português. |
| `Procfile` / `railway.json` | Dizem ao Railway como rodar o bot. |
| `.github/workflows/bot.yml` | Disparo manual de reserva pelo GitHub. |
| `vistos.txt` | Memória: os produtos que ele já avisou. Criado sozinho. |
| `requirements.txt` | Lista do que o GitHub precisa instalar antes de rodar. |
| `.env.example` | Modelo de configuração. Só serve se você rodar no seu Mac. |
| `--configurar-github` | Comando que guarda as senhas no cofre do GitHub. |
| `.gitignore` | Impede que senhas subam para o GitHub por acidente. |

**O webhook não fica em nenhum arquivo.** Ele mora no cofre de senhas do
GitHub (*Settings → Secrets*) e só é injetado na hora que o bot roda.

---

## Produtos esgotados não são anunciados

A maioria dos produtos do CSSDeals tem **uma única unidade** — o campo
`quantity` vem `1` na maior parte deles. Por isso esgotam em minutos.

O bot confere o estoque em dois momentos:

1. **Ao ler** — produto que já está com `quantity: 0` nem entra na fila
2. **Antes de enviar** — reconfere, porque uma leva grande leva dezenas
   de segundos para sair e dá tempo de esgotar na espera

A segunda conferência não custa requisição extra: aproveita a mesma
chamada que busca a foto do anúncio.

> **Medido em 500 produtos:** 4% estavam esgotados, e **21 dos 22 estavam
> entre os 100 mais recentes** — ou seja, esgotam logo depois de publicados,
> que é exatamente quando o bot avisaria.

Se a consulta de estoque falhar por problema de rede, o item é enviado
mesmo assim — melhor avisar de um item que talvez tenha acabado do que
deixar de avisar por causa de uma consulta que não respondeu.

---

## Como ele evita te avisar duas vezes do mesmo item

Cada produto do CSSDeals tem um número de identificação próprio. O bot guarda
esses números no arquivo `vistos.txt` e, antes de avisar, confere se o número
já está lá.

**Você nunca recebe o mesmo produto duas vezes**, mesmo que ele mude de preço
ou de foto.

O número só entra na lista **depois** que a mensagem foi enviada com sucesso.
Ou seja: se o Discord estiver fora do ar, o produto continua contando como
"não avisado" e entra de novo na rodada seguinte — **você não perde nenhum
lançamento**.

O arquivo guarda os 1.000 mais recentes e ocupa uns 20 KB. Você pode abri-lo
no GitHub e ler, se quiser.

---

## Problemas comuns

**A rodada aparece com bolinha vermelha na aba Actions**
Clique nela para ver o erro. As causas mais comuns estão abaixo.

**"Nenhum canal de notificacao configurado"**
Os Secrets não foram criados. Rode `bot.py --configurar-github` de novo.
Os nomes precisam ser exatamente `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
(ou `DISCORD_WEBHOOK_URL`).

**"TELEGRAM SEM PERMISSAO (403)"**
O bot foi removido do grupo, ou o ID está errado. Rode
`bot.py --configurar-github` de novo — ele redescobre o grupo.

**"Nao achei nenhum grupo"** (durante a configuração)
Faltou mandar uma mensagem no grupo **depois** de adicionar o bot. O Telegram
só mostra o grupo pro bot a partir da primeira mensagem.

**"DISCORD recusou (404)"**
A URL do webhook está errada, incompleta ou foi apagada no Discord. Crie um
webhook novo e atualize o Secret.

**"Nenhum produto veio na resposta"**
A API do site mudou. Me chame que eu ajusto.

**Não chega nada, mas as rodadas estão verdes**
Normal na primeira rodada — ela só guarda os produtos atuais como ponto de
partida. A partir da segunda você recebe os lançamentos novos. Como o site
publica ~2 produtos a cada 15 minutos, pode haver rodadas sem nada.

**As rodadas pararam sozinhas depois de semanas**
O GitHub desativa o agendamento de repositórios sem atividade por 60 dias.
Ele avisa por e-mail; basta reativar na aba **Actions**.

---

## Canais separados por categoria no Discord

Dá para ter um canal por tipo de produto, além de um canal geral com tudo.
Crie um webhook em cada canal e cole nas Variables do Railway:

| Variável | Canal recebe |
|---|---|
| `DISCORD_WEBHOOK_URL` | **tudo** (canal geral) |
| `CANAL_CALCADOS` | Shoes |
| `CANAL_ROUPAS` | T-shirts, Pants, Hoodie, Coat, suit, socks... |
| `CANAL_ACESSORIOS` | Hat&Bags, Belt&Glasses, perfume, suitcase... |
| `CANAL_ELETRONICOS` | Watches, Cell phone, Earphone, phone case... |
| `CANAL_OUTROS` | sports goods, toy e o que não se encaixar |

Você não precisa digitar número de categoria — o mapa das 29 categorias do
site já está pronto no `bot.py`. Preencha só os canais que quiser; os
demais podem ficar em branco.

Um mesmo produto vai para o canal geral **e** para o canal da categoria
dele, se os dois estiverem configurados.

> **Sobre o volume:** ele é bem desigual. Numa amostra de 40 lançamentos,
> Roupas levou 28, Acessórios 9, Outros 2 e Calçados 1. Canal que quase
> nunca recebe nada passa impressão de abandono — vale considerar juntar
> os menores.

---

## Quer receber só de uma aba?

Por padrão você recebe lançamentos de **todas** as abas. Para receber só de
uma (ex: só tênis), no repositório vá em:

**Settings** → **Secrets and variables** → **Actions** → aba **Variables** →
**New repository variable**

- **Name:** `CATEGORIA_ID`
- **Value:** `11`  *(11 = Shoes)*

> Atenção: é a aba **Variables**, não *Secrets* — não é uma senha.

Alguns números: `11` Shoes · `32` Hoodie · `14` T-shirts · `15` Pants ·
`12` Coat · `20` Watches · `26` Hat&Bags · `36` sports goods.
A lista completa das 29 abas está no `.env.example`.

---

## Por que não existe "preço original riscado"

O CSSDeals guarda **um preço só** por produto — não há campo de preço
antigo, e `discount` vem sempre zerado. Não existe o que riscar.

Buscar o valor no anúncio de origem foi testado e **descartado**:

- **Taobao** (20% dos produtos): o `robots.txt` proíbe explicitamente
  qualquer acesso automatizado que não seja do Googlebot
- **1688** (52%): a primeira consulta funciona, mas as seguintes vêm
  bloqueadas por proteção anti-bot (captcha/punish). Funcionaria hoje e
  quebraria amanhã, sem aviso

---

## Ajustes que dá pra fazer (é só pedir)

- **Filtrar por preço** (ex: só avisar coisas abaixo de CN¥ 50)
- **Mudar o intervalo** das rodadas
- **Derrubar o atraso para menos de 1 minuto** (exige servidor pago, ~R$ 30/mês)
- **Filtrar por palavra-chave** no título (ex: só avisar se tiver "Nike")
- **Converter os preços** de CN¥ para real
