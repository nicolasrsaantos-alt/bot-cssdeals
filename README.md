# Bot de Coleta + Notificação

Bot que vigia o **cssdeals.com** e **te avisa no Telegram (ou Discord) toda
vez que um produto novo é lançado** — com título traduzido para português,
foto e link.

Roda **na nuvem do GitHub**, de graça. Seu computador pode ficar desligado.

Escrito para quem não é programador. É só seguir os passos na ordem.

---

## ✅ Como o bot monitora os lançamentos (leia, é rápido)

Você comentou que o site tem várias abas (**Shoes**, **Hoodie**, **Pants**...),
cada uma com subabas de tamanho, 20 produtos por página e às vezes mais de 20
páginas. Varrer tudo isso daria **centenas de requisições** a cada rodada — e
ainda correria o risco de perder algum lançamento.

**Não é preciso.** Investigando o site, descobri que ele tem uma API interna
que devolve os produtos **já ordenados do mais novo para o mais antigo**,
misturando todas as abas e tamanhos.

Como você só quer **lançamentos**, basta ler a primeira página dessa lista: o
que apareceu de novo desde a última vez está sempre no topo.

| | Varrer aba por aba | O que o bot faz |
|---|---|---|
| Requisições por rodada | centenas | **1** |
| Cobre todas as abas | só as configuradas | **todas, sempre** |
| Peso no site | alto | mínimo |

O bot lê os **50 produtos mais recentes** a cada rodada. O site cadastra
bem menos que isso nesse intervalo, então sobra folga. Se algum dia todos os
50 forem novos, o bot te avisa no log que talvez tenha escapado algum.

**Confirmado no site:** catálogo com **~10.000 produtos**, com itens sendo
cadastrados **hoje**. Os produtos vêm de Taobao, Weidian e 1688.

### O que chega no seu grupo

```
🆕 Camisa Polo Masculina Novo Verão 2025 Camiseta Patchwork Top
2025 New Summer Men's Polo Shirt T-Shirt Patchwork Top          ← original
T-shirts · Weidian
Preço: CN¥ 30.19
Ver no CSSDeals    ← link clicável
[foto do produto]
```

**Os títulos são traduzidos automaticamente para português.** O original vem
logo abaixo, em itálico — útil pra procurar o produto no site.

A tradução usa o MyMemory, que é gratuito e **não precisa de cadastro nem
senha**. Detalhes honestos sobre ele:

- Títulos em **chinês** traduzem bem — que é o caso que mais atrapalha
- Alguns títulos em **inglês** voltam sem tradução (limitação do serviço
  gratuito). Como já eram legíveis, não atrapalha
- Cada tradução fica **guardada no banco**. O mesmo título nunca é traduzido
  duas vezes, o que economiza a cota
- Se a tradução falhar ou a cota do dia acabar, **você continua recebendo a
  notificação**, só que com o título original. Nunca deixa de avisar

Para desligar a tradução, coloque `TRADUZIR=nao` no `.env`.
Se a cota apertar, preencha `TRADUCAO_EMAIL=seuemail@exemplo.com` no `.env` —
aumenta bastante o limite diário (não precisa criar conta, é só informar).

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

**Na nuvem do GitHub — não no seu Mac.** Seu MacBook pode ficar desligado.

O agendador local foi desativado de propósito: se os dois rodassem juntos,
eles teriam memórias separadas e você receberia **cada produto duas vezes**.

---

## Como colocar no ar (uma vez só, ~10 minutos)

### 1. Criar conta no GitHub (se ainda não tem)

Acesse **github.com** → *Sign up*. É gratuito e não pede cartão.

### 2. Criar o repositório

Em **github.com/new**:

- **Repository name:** `bot-cssdeals`
- **Visibilidade:** marque **Public** ⚠️

> **Por que Public?** O GitHub dá minutos de execução **ilimitados** para
> repositórios públicos, e só 2.000 min/mês nos privados. Rodando a cada 5
> minutos, o limite privado estouraria em poucos dias.
>
> **Não tem risco:** o webhook não fica no código — ele vai pro cofre de
> senhas do GitHub (próximo passo). O que fica público é só o programa e a
> lista de números de produtos já avisados.

- **Não marque** nenhuma opção de "Add README/.gitignore/license"
- Clique em **Create repository**

### 3. Enviar o código

Na página que aparece, copie seu endereço (algo como
`https://github.com/SEU-USUARIO/bot-cssdeals.git`) e cole no Terminal,
trocando pelo seu:

```bash
cd /Users/nicolas/bot-coleta && git remote add origin https://github.com/SEU-USUARIO/bot-cssdeals.git && git push -u origin main
```

O GitHub vai pedir login. Se pedir senha, ele quer um **token**, não sua
senha — ele mesmo mostra o link para gerar.

### 4. Guardar as senhas no cofre

Não precisa mexer no site do GitHub. Um comando faz tudo:

```bash
cd /Users/nicolas/bot-coleta && .venv/bin/python bot.py --configurar-github
```

Ele pergunta o repositório, se você quer Telegram ou Discord, e guarda as
senhas direto no cofre do GitHub.

#### Se escolher Telegram

Prepare estas duas coisas antes:

**a) O token do bot**

1. No Telegram, procure **@BotFather** (tem selo azul de verificado)
2. Mande `/newbot`
3. Ele pergunta o **nome** — pode ser qualquer um, ex: `Avisos CSSDeals`
4. Ele pergunta o **username** — precisa terminar em `bot`, ex: `avisoscss_bot`
5. Ele responde com o token, algo como `7891234567:AAHk9x-abcdefGHIJKL`

**b) O grupo com o bot dentro**

1. Crie um grupo no Telegram
2. Nome do grupo no topo → **Adicionar Membros** → procure o username do seu
   bot → adicione
3. **Mande qualquer mensagem no grupo** (ex: "oi") — isso é obrigatório

Pronto. O assistente **descobre o ID do grupo sozinho** — era a parte mais
confusa do processo e você não precisa fazer nada.

#### Se escolher Discord

Canal → engrenagem (Editar Canal) → **Integrações** → **Webhooks** →
**Novo webhook** → **Copiar URL do Webhook**. Cole quando ele pedir.

#### Como ele se comporta

O assistente **manda uma mensagem de teste antes de salvar**. Se ela não
chegar, ele **não guarda nada** e te diz o motivo — assim você nunca fica com
uma configuração quebrada achando que está tudo certo.

> As senhas vão do seu Terminal direto para o cofre do GitHub. Não ficam
> salvas em arquivo nenhum na sua máquina.

### 5. Ligar

Aba **Actions** do repositório → se pedir, clique em
**I understand my workflows, go ahead and enable them** → escolha
**Monitor CSSDeals** na esquerda → **Run workflow** → **Run workflow**.

A primeira rodada **não envia mensagem** (guarda os 50 produtos atuais como
ponto de partida). Da segunda em diante chegam só os lançamentos novos.

---

## Acompanhando

- **Ver as rodadas:** aba **Actions** do repositório. Bolinha verde = rodou bem.
- **Ver o que ele já avisou:** arquivo `vistos.txt` no repositório.
- **Forçar uma rodada agora:** Actions → Monitor CSSDeals → **Run workflow**.
- **Pausar:** Actions → Monitor CSSDeals → botão `...` → **Disable workflow**.

### Sobre o atraso

O GitHub agenda para cada 5 minutos, mas **na prática atrasa** — em horário
de pico pode levar de 5 a 40 minutos. É limitação da plataforma gratuita,
não do bot.

**Nenhum produto é perdido por causa disso.** O bot lê os 50 mais recentes,
o que cobre cerca de 6 horas de publicações do site. Mesmo um atraso de uma
hora não deixa nada escapar — o aviso só chega mais tarde.

Se um dia o atraso incomodar, a solução é um servidor pequeno sempre ligado
(~R$ 30/mês), onde o delay cai para menos de 1 minuto. É só pedir.

---

## O que cada arquivo faz

| Arquivo | Para que serve |
|---|---|
| `bot.py` | O programa. Todo comentado em português. |
| `.github/workflows/bot.yml` | Diz ao GitHub quando e como rodar o bot. |
| `vistos.txt` | Memória: os produtos que ele já avisou. Criado sozinho. |
| `requirements.txt` | Lista do que o GitHub precisa instalar antes de rodar. |
| `.env.example` | Modelo de configuração. Só serve se você rodar no seu Mac. |
| `--configurar-github` | Comando que guarda as senhas no cofre do GitHub. |
| `.gitignore` | Impede que senhas subam para o GitHub por acidente. |

**O webhook não fica em nenhum arquivo.** Ele mora no cofre de senhas do
GitHub (*Settings → Secrets*) e só é injetado na hora que o bot roda.

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

## Ajustes que dá pra fazer (é só pedir)

- **Filtrar por preço** (ex: só avisar coisas abaixo de CN¥ 50)
- **Mudar o intervalo** das rodadas
- **Derrubar o atraso para menos de 1 minuto** (exige servidor pago, ~R$ 30/mês)
- **Filtrar por palavra-chave** no título (ex: só avisar se tiver "Nike")
- **Converter os preços** de CN¥ para real
