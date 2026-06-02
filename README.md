# 🚴 Bike Monitor — Victor

Monitora **OLX**, **BazarBikes** e **Semexe** por bikes speed/road usadas e
componentes Shimano Deore M6100 1x12. Roda automaticamente via **GitHub Actions**
a cada 6 horas. Envia e-mail HTML com os novos anúncios.

Análise inteligente opcional via Claude API (identifica melhores negócios).

---

## Setup em 15 minutos

### Passo 1 — Criar o repositório no GitHub

1. Acesse https://github.com/new
2. Nome: `bike-monitor` (privado — seus segredos ficam seguros)
3. Clique **Create repository**

### Passo 2 — Subir os arquivos

Na raiz do repositório, crie a seguinte estrutura:

```
bike-monitor/
├── scraper.py
├── requirements.txt
└── .github/
    └── workflows/
        └── bike-monitor.yml
```

**Via GitHub web UI** (sem instalar git):
- Clique **Add file > Create new file**
- Para o workflow, digite o caminho completo: `.github/workflows/bike-monitor.yml`
- Cole o conteúdo de cada arquivo

### Passo 3 — Configurar o Gmail (App Password)

O script usa Gmail com App Password (não sua senha normal).

1. Acesse https://myaccount.google.com/security
2. Ative **Verificação em 2 etapas** (obrigatório)
3. Em **Senhas de app** → crie uma senha para "Bike Monitor"
4. Você receberá uma senha de 16 caracteres tipo `abcd efgh ijkl mnop`
   (copie sem os espaços: `abcdefghijklmnop`)

### Passo 4 — Adicionar os Secrets no GitHub

No repositório → **Settings > Secrets and variables > Actions > New repository secret**

| Secret | Valor | Obrigatório |
|--------|-------|-------------|
| `EMAIL_FROM` | `seu_email@gmail.com` | ✅ |
| `EMAIL_PASS` | App Password de 16 chars | ✅ |
| `EMAIL_TO` | `seu_email@gmail.com` | ✅ |
| `PRECO_MAX` | `15000` (R$ máximo, 0=sem limite) | ✅ |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | ⬜ opcional |

### Passo 5 — Testar manualmente

1. No repositório → aba **Actions**
2. **Bike Monitor** → **Run workflow** → **Run**
3. Acompanhe os logs em tempo real
4. Verifique sua caixa de e-mail

---

## Personalização

### Adicionar/remover termos de busca no OLX

No `scraper.py`, edite a lista `BUSCA_OLX`:

```python
BUSCA_OLX = [
    "cannondale CAAD10",
    "cannondale CAAD13",
    "trek emonda",
    "deore m6100",
    # adicione aqui o que quiser
]
```

### Mudar o limite de preço

Altere o Secret `PRECO_MAX` no GitHub (ou diretamente no `scraper.py`).

### Mudar a frequência

No arquivo `.github/workflows/bike-monitor.yml`, edite o cron:

```yaml
# A cada 4 horas:
- cron: "0 */4 * * *"

# A cada 12 horas:
- cron: "0 6,18 * * *"

# Uma vez por dia às 7h BRT (10h UTC):
- cron: "0 10 * * *"
```

> **Nota**: GitHub Actions usa UTC. BRT = UTC-3.

### Adicionar outros sites

O arquivo `scraper.py` tem funções modulares. Para adicionar um novo site:
1. Crie uma função `scrape_novosite(urls)` seguindo o padrão das existentes
2. Chame-a no `main()` e estenda `all_listings`

---

## Limitações conhecidas

### OLX
O OLX Brasil implementou proteção anti-bot agressiva em 2024. Os IPs dos
servidores GitHub Actions são identificados como datacenter e podem receber 403.

**Sintoma**: log mostra `HTTP 403 em olx.com.br`.

**Soluções**:
1. **Aceitar a taxa de falha** (~30-40%) — ainda captura parte dos anúncios
2. **ScraperAPI** (gratuito até 1.000 requests/mês): basta trocar a URL no `get()`:
   ```python
   url_proxy = f"http://api.scraperapi.com?api_key=SUA_KEY&url={requests.utils.quote(url)}"
   r = requests.get(url_proxy, timeout=30)
   ```
3. Para monitoramento sério do OLX, BazarBikes + Semexe já cobrem bem o mercado SP.

### Semexe
O seletor CSS pode mudar com atualizações do tema. Se parar de capturar,
inspecione a página e atualize os seletores em `scrape_semexe()`.

### Estado (seen_ids.json)
O GitHub Actions cache tem limite de 10GB e expira após 7 dias de inatividade.
Se o cache expirar, o script reenviará anúncios antigos uma vez. Não é crítico.

---

## Logs

Cada run mostra:
```
10:00:01 [INFO] Bike Monitor iniciando...
10:00:01 [INFO] Anúncios já vistos: 47
10:00:02 [INFO] BazarBikes: speed — 12 anúncios relevantes
10:00:05 [INFO] Semexe bikes/estrada — 8 anúncios relevantes
10:00:08 [INFO] OLX 'cannondale CAAD10' — 3 relevantes
10:00:25 [INFO] Novos anúncios: 2
10:00:27 [INFO] E-mail enviado: 🚴 2 novas bikes no radar — 31/05/2025 10:00
```

---

## Estrutura do projeto

```
scraper.py              — lógica principal
requirements.txt        — dependências Python
seen_ids.json           — gerado automaticamente (cache de IDs vistos)
.github/
  workflows/
    bike-monitor.yml    — agendamento GitHub Actions
```
