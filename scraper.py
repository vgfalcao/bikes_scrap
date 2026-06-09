#!/usr/bin/env python3
"""
Bike Monitor — Victor (vgfalcao@gmail.com)
Monitora OLX, BazarBikes e Semexe por bikes speed/road e MTB 29 completas.
Roda via GitHub Actions 2x/semana (ter + sex, 10h BRT).
Score principal (100 pts) + Nota de Valor Percebido (100 pts).
Notifica apenas se score >= 65 E vp >= 60.
"""

import os, json, time, hashlib, smtplib, logging, re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from db_matcher import load_db, enrich_from_db

# ──────────────────────────────────────────────────────────────
# CONFIGURAÇÃO
# ──────────────────────────────────────────────────────────────

CONFIG = {
    "email_from":  os.environ.get("EMAIL_FROM",  "seu_email@gmail.com"),
    "email_pass":  os.environ.get("EMAIL_PASS",  ""),
    "email_to":    os.environ.get("EMAIL_TO",    "vgfalcao@gmail.com"),
    "preco_max":   int(os.environ.get("PRECO_MAX", "25000")),
    "state_file":  "seen_ids.json",
    "bench_file":  "benchmarks.json",
    "delay":       2.5,
    "score_min":   50,
    "vp_min":      65,
}

# ──────────────────────────────────────────────────────────────
# BENCHMARKS EMBUTIDOS (fallback se benchmarks.json ausente)
# Atualizar via benchmarks_updater.py 1x/mês
# ──────────────────────────────────────────────────────────────

BENCHMARKS_DEFAULT = {
    "updated_at": "2025-06-01",
    "confidence": "high",
    "speed": {
        "alu_105":           {"p25": 3800,  "median": 5200,  "p75": 6400,  "novo_loja": 11900, "novo_ml": 9800},
        "alu_ultegra":       {"p25": 5500,  "median": 7500,  "p75": 9500,  "novo_loja": 16000, "novo_ml": 13500},
        "alu_rival":         {"p25": 4500,  "median": 6000,  "p75": 7500,  "novo_loja": 13000, "novo_ml": 11000},
        "carbono_105":       {"p25": 6800,  "median": 9000,  "p75": 11500, "novo_loja": 18500, "novo_ml": 15500},
        "carbono_ultegra":   {"p25": 9500,  "median": 13000, "p75": 16000, "novo_loja": 26000, "novo_ml": 22000},
        "carbono_di2":       {"p25": 14000, "median": 19000, "p75": 25000, "novo_loja": 42000, "novo_ml": 36000},
    },
    "mtb": {
        "alu_slx_rockshox":  {"p25": 4800,  "median": 6800,  "p75": 8500,  "novo_loja": 14000, "novo_ml": 12000},
        "alu_xt_fox":        {"p25": 6500,  "median": 9000,  "p75": 11000, "novo_loja": 18000, "novo_ml": 15000},
        "carbono_slx":       {"p25": 7000,  "median": 10000, "p75": 13000, "novo_loja": 20000, "novo_ml": 17000},
        "carbono_xt_fox":    {"p25": 9000,  "median": 12000, "p75": 15000, "novo_loja": 28000, "novo_ml": 24000},
        "carbono_xtr_eagle": {"p25": 13000, "median": 18000, "p75": 24000, "novo_loja": 40000, "novo_ml": 34000},
    },
    # Tier A = importadas premium, Tier B = nacionais premium, Tier C = nacionais intermediárias
    "tiers": {
        "speed": {
            "A": {"marcas": ["specialized","trek","cannondale","scott","giant","cervelo","pinarello","bmc","colnago"],
                  "fator_novo": 1.0},
            "B": {"marcas": ["merida","cube","fuji","focus","bh","kona","lapierre"],
                  "fator_novo": 0.75},
            "C": {"marcas": ["caloi","oggi","houston","audax","groove","absolute","first"],
                  "fator_novo": 0.55},
        },
        "mtb": {
            "A": {"marcas": ["specialized","trek","santa cruz","yeti","scott","canyon","orbea","norco"],
                  "fator_novo": 1.0},
            "B": {"marcas": ["sense","oggi","caloi","audax","groove","kona","transition"],
                  "fator_novo": 0.65},
            "C": {"marcas": ["absolute","first","houston","caloi entry","oggi entry"],
                  "fator_novo": 0.45},
        },
    }
}

# ──────────────────────────────────────────────────────────────
# FILTROS ELIMINATÓRIOS
# ──────────────────────────────────────────────────────────────

FILTROS_SPEED = {
    "grupos_ok":    ["105","r7000","r7100","r8000","r8100","ultegra","dura-ace","di2","rival","force","red","etap","axs"],
    "grupos_nok":   ["tiagra","sora","claris","tourney","altus","acera","alivio","deore","1x8","1x9","2x8","2x9"],
    "tamanhos_ok":  ["50","52","54","56"],
    "ano_min_alu":  2015,   # alumínio: 2015+
    "ano_min_carb": 2010,   # carbono tier A (Pinarello, Colnago, etc): 2010+
}

FILTROS_MTB = {
    "grupos_ok":    ["deore m6100","slx","m7100","xt","m8100","xtr","m9100","nx eagle","gx eagle","x01","xx1","sram"],
    "grupos_nok":   ["deore m5100","deore m4100","altus","acera","alivio","tourney","1x8","1x9","2x8","2x9","3x"],
    "tamanhos_ok":  ["m","17","17.5","18","19","l"],
    "aros_ok":      ["29","29er"],
}

KEYWORDS_DESCARTAR = ["infantil","criança","kids","bmx","motorizada","elétrica","eletrica",
                      "patinete","trotinete","spinning","ergométrica","dobrável","dobravel",
                      "speed levado a serio","mtb levado a serio","mountain bike levado",
                      "speed levado","levado a sério"]

# ──────────────────────────────────────────────────────────────
# MAPEAMENTO DE MARCAS → TIERS E MODELOS CONHECIDOS
# ──────────────────────────────────────────────────────────────

MODELOS_SPEED = {
    "cannondale": ["caad10","caad13","supersix","synapse","topstone","systemsix"],
    "trek":       ["emonda","domane","madone","checkpoint"],
    "specialized":["tarmac","roubaix","allez","diverge","aethos"],
    "giant":      ["tcr","defy","contend","propel"],
    "scott":      ["addict","foil","speedster","solace"],
    "cervelo":    ["r3","r5","s3","s5"],
    "merida":     ["scultura","reacto","ride","silex"],
    "cube":       ["agree","attain","litening"],
    "fuji":       ["transonic","sl","roubaix"],
    "bh":         ["g7","ultralight","rx"],
}

MODELOS_MTB = {
    "specialized": ["stumpjumper","stumpy","fuse","epic","enduro","camber"],
    "trek":        ["fuel","slash","remedy","roscoe","marlin 8","supercaliber"],
    "santa cruz":  ["hightower","tallboy","bronson","megatower","5010"],
    "scott":       ["spark","genius","scale","ransom"],
    "sense":       ["exper","impact","invictus","ultra","react sport","react evo"],
    "oggi":        ["big wheel","agile","7.3","7.4","stinger"],
    "caloi":       ["elite carbon","elite"],
    "canyon":      ["neuron","spectral","torque","sender"],
    "orbea":       ["occam","rallon","laufey","onna"],
    "yeti":        ["sb130","sb140","sb150","sb115"],
}

# ──────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# ESTADO
# ──────────────────────────────────────────────────────────────

def load_seen() -> set:
    p = Path(CONFIG["state_file"])
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()

def save_seen(seen: set):
    Path(CONFIG["state_file"]).write_text(json.dumps(list(seen), indent=2))

def load_benchmarks() -> dict:
    p = Path(CONFIG["bench_file"])
    if p.exists():
        try:
            data = json.loads(p.read_text())
            log.info(f"Benchmarks carregados: {data.get('updated_at','?')} ({data.get('confidence','?')})")
            return data
        except Exception:
            pass
    log.warning("benchmarks.json ausente ou inválido — usando defaults embutidos")
    return BENCHMARKS_DEFAULT

def make_id(source: str, raw_id: str) -> str:
    return f"{source}:{raw_id}"

# ──────────────────────────────────────────────────────────────
# HELPERS DE PARSING
# ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Headers específicos para Semexe
HEADERS_SEMEXE = {
    **HEADERS,
    "Referer": "https://www.semexe.com/bikes/",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}

# ── Firecrawl ──────────────────────────────────────────────────
# Free tier: 1.000 créditos/mês — ~376 créditos/mês para 2x/semana.
# Cadastro: https://www.firecrawl.dev (Free plan, sem cartão)
# Adicionar FIRECRAWL_API_KEY nos Secrets do GitHub.
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
FIRECRAWL_BASE    = "https://api.firecrawl.dev/v1"

def firecrawl_scrape(url: str) -> requests.Response | None:
    """
    Usa Firecrawl /scrape para buscar uma URL contornando bloqueios anti-bot.
    Retorna um objeto Response-like com .text e .status_code para manter
    compatibilidade com o resto do código.
    Consome 1 crédito por chamada.
    """
    if not FIRECRAWL_API_KEY:
        return None
    try:
        resp = requests.post(
            f"{FIRECRAWL_BASE}/scrape",
            headers={
                "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "url":     url,
                "formats": ["markdown","html"],  # markdown para parsing, html como fallback
                "onlyMainContent": False,
                "timeout": 20000,
            },
            timeout=40,
        )
        if resp.status_code != 200:
            log.warning(f"Firecrawl HTTP {resp.status_code} para {url[:60]}")
            return None
        data = resp.json()
        if not data.get("success"):
            log.warning(f"Firecrawl erro para {url[:60]}: {data.get('error','?')}")
            return None

        # Monta objeto compatível com requests.Response
        class _R:
            def __init__(self, html, md):
                self.text        = html or md or ""
                self.status_code = 200
            def json(self):
                import json as _j
                return _j.loads(self.text)

        html = data.get("data",{}).get("html","")
        md   = data.get("data",{}).get("markdown","")
        return _R(html, md)
    except Exception as e:
        log.warning(f"Firecrawl exceção para {url[:60]}: {e}")
        return None


def get_proxied(url: str, retries: int = 3, **kwargs):
    """
    Tenta Firecrawl primeiro (contorna anti-bot).
    Se não houver chave ou falhar, tenta requests direto.
    """
    # Tenta Firecrawl
    if FIRECRAWL_API_KEY:
        r = firecrawl_scrape(url)
        if r:
            return r
        log.warning(f"Firecrawl falhou para {url[:60]} — tentando direto")

    # Fallback: requests direto (funciona para sites sem anti-bot)
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
            if r.status_code == 200:
                return r
            log.warning(f"HTTP {r.status_code} em {url[:60]} (tentativa {attempt+1})")
        except Exception as e:
            log.warning(f"Erro em {url[:60]}: {e} (tentativa {attempt+1})")
        time.sleep(CONFIG["delay"] * (attempt + 1))
    return None

def get(url: str, retries: int = 3, headers: dict = None, **kwargs):
    h = headers or HEADERS
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=h, timeout=20, **kwargs)
            if r.status_code == 200:
                return r
            log.warning(f"HTTP {r.status_code} em {url} (tentativa {attempt+1})")
        except Exception as e:
            log.warning(f"Erro em {url}: {e} (tentativa {attempt+1})")
        time.sleep(CONFIG["delay"] * (attempt + 1))
    return None

def parse_price(s: str) -> int | None:
    """Trata formatos: R$ 6.499,00 | R$ 6.499 | 6499 | R$23.500"""
    s = str(s).replace("R$","").replace("R$","").strip()
    # Remove centavos BR: 6.499,00 → 6.499
    s = re.sub(r",[0-9]{1,2}$", "", s.strip())
    # Remove ponto de milhar: 6.499 → 6499
    s = s.replace(".","").strip()
    nums = re.sub(r"[^0-9]", "", s)
    return int(nums) if nums else None

def price_ok(price: int | None) -> bool:
    if price is None:
        return True
    if CONFIG["preco_max"] > 0 and price > CONFIG["preco_max"]:
        return False
    return True

def norm(s: str) -> str:
    return s.lower().strip()

def text_contains_any(text: str, keywords: list) -> bool:
    t = norm(text)
    return any(k in t for k in keywords)

def extract_year(text: str) -> int | None:
    m = re.search(r"\b(201[5-9]|202[0-9])\b", text)
    return int(m.group(1)) if m else None

def extract_weight(text: str) -> float | None:
    m = re.search(r"(\d+[,.]?\d*)\s*kg", norm(text))
    if m:
        return float(m.group(1).replace(",", "."))
    return None

def extract_size(text: str) -> str | None:
    t = norm(text)
    # tam 54, tamanho 52, size m, tam m, etc.
    m = re.search(r"(?:tam(?:anho)?|size)[.\s]*([a-z0-9/]+)", t)
    if m:
        return m.group(1).strip()
    # standalone: "54cm", "tam 54"
    m = re.search(r"\b(4[6-9]|5[0-8]|x[sl]|[sml])\b", t)
    return m.group(1) if m else None

# ──────────────────────────────────────────────────────────────
# EXTRAÇÃO DE ATRIBUTOS DO TEXTO LIVRE
# ──────────────────────────────────────────────────────────────

def detect_category(title: str, desc: str = "") -> str | None:
    """Retorna 'speed' ou 'mtb' ou None."""
    t = norm(title + " " + desc)
    speed_kw = ["speed","estrada","road","gravel","drop","105","ultegra","dura-ace",
                "caad","supersix","tarmac","emonda","tcr","synapse","roubaix","addict",
                "rimbrake","rim brake","pedivela compacto"]
    mtb_kw   = ["mtb","mountain","trail","enduro","29er","aro 29","trilha",
                "stumpjumper","fuel ex","spark","sense exper","hightower",
                "lefty","scalpel","fsi","scale","spark rc","genius","neuron",
                "impact","big wheel","agile","oggi 29","oggi mtb",
                "xtr","slx","deore m","xt m8","xt m9","gx eagle","nx eagle",
                "x01","xx1","rockshox pike","rockshox lyrik","fox 34","fox 36",
                "suspensao ar","canote retratil","dropper"]
    if text_contains_any(t, KEYWORDS_DESCARTAR):
        return None
    s = sum(1 for k in speed_kw if k in t)
    m = sum(1 for k in mtb_kw   if k in t)
    if s > m:  return "speed"
    if m > s:  return "mtb"
    if s > 0:  return "speed"
    if m > 0:  return "mtb"
    return None

def detect_brand(text: str, category: str) -> str | None:
    t = norm(text)
    pool = MODELOS_SPEED if category == "speed" else MODELOS_MTB
    for brand in pool:
        if brand in t:
            return brand
    return None

def detect_grupo_speed(text: str) -> str | None:
    t = norm(text)
    for g in ["dura-ace di2","ultegra di2","105 di2","sram red etap","sram force etap",
              "sram rival etap","dura-ace","ultegra r8100","ultegra r8000","ultegra",
              "105 r7100","105 r7000","105 5800","105","sram force","sram rival","sram red"]:
        if g in t:
            return g
    return None

def detect_grupo_mtb(text: str) -> str | None:
    t = norm(text)
    for g in ["xtr m9100","xtr","xx1 axs","xx1 eagle","xx1","x01 axs","x01 eagle","x01",
              "gx axs","gx eagle","gx","nx eagle","nx","xt m8100","xt m8000","xt",
              "slx m7100","slx","deore m6100","deore"]:
        if g in t:
            return g
    return None

def detect_material(text: str) -> str:
    t = norm(text)
    # Indicadores explícitos de quadro alumínio — têm prioridade
    # mesmo que o garfo seja carbono (ex: Trek Emonda ALR, CAAD10)
    alu_signals = ["aluminum","aluminium","alpha aluminum","smartform","ultralight 300",
                   "ultralight 500","alr","caad","6061","6069","7005","aluminio"]
    if any(k in t for k in alu_signals):
        return "aluminio"
    if any(k in t for k in ["carbono","carbon","hi-mod","himod","oclv","ballistec","fact"]):
        if any(k in t for k in ["hi-mod","himod","ballistec","oclv 700","fact 12"]):
            return "carbono_himod"
        return "carbono"
    return "aluminio"

def detect_suspensao(text: str) -> str | None:
    t = norm(text)
    for s in ["fox 36 factory","fox 34 factory","fox factory",
              "fox 36 rhythm","fox 34 rhythm","fox rhythm",
              "rockshox pike ulti","rockshox pike select","rockshox pike",
              "rockshox lyrik","rockshox sid sl","rockshox sid","rockshox recon",
              "sr suntour","suntour"]:
        if s in t:
            return s
    return None

def detect_rodas(text: str) -> str | None:
    t = norm(text)
    for r in ["zipp","enve","roval carbon","dt swiss xrc","mavic cosmic carbon",
              "ksyrium pro carbon","fulcrum racing zero carbon",
              "mavic ksyrium","dt swiss r470","dt swiss","fulcrum racing",
              "bontrager paradigm elite","vision","roval control"]:
        if r in t:
            return r
    return None

def detect_canote(text: str) -> tuple[bool, int]:
    """Retorna (tem_canote, curso_mm)."""
    t = norm(text)
    if not any(k in t for k in ["canote retrat","dropper","seatpost retrat","bike yoke","reverb","command post"]):
        return False, 0
    m = re.search(r"(\d{2,3})\s*mm", t)
    curso = int(m.group(1)) if m else 125
    return True, curso

def detect_nf(text: str) -> bool:
    t = norm(text)
    return any(k in t for k in ["nota fiscal","com nf","acompanha nf","nf brasil","nf original"])

# ──────────────────────────────────────────────────────────────
# TIER LOOKUP
# ──────────────────────────────────────────────────────────────

def get_tier(brand: str | None, category: str, benchmarks: dict) -> str:
    if not brand:
        return "C"
    b = norm(brand)
    tiers = benchmarks.get("tiers", BENCHMARKS_DEFAULT["tiers"]).get(category, {})
    for tier_name in ["A", "B", "C"]:
        if b in tiers.get(tier_name, {}).get("marcas", []):
            return tier_name
    return "C"

def get_novo_reference(brand: str | None, category: str, material: str,
                       grupo_tier: str, benchmarks: dict) -> dict:
    """
    Retorna {"novo_loja": X, "novo_ml": Y} ajustado pelo tier da marca.
    grupo_tier: chave base do benchmark (ex: 'carbono_ultegra', 'carbono_xt_fox')
    """
    cat_bench = benchmarks.get(category, BENCHMARKS_DEFAULT.get(category, {}))
    base = cat_bench.get(grupo_tier, {})
    if not base:
        # fallback mais próximo
        keys = list(cat_bench.keys())
        base = cat_bench.get(keys[0], {"novo_loja": 15000, "novo_ml": 12000})

    tier = get_tier(brand, category, benchmarks)
    tiers_data = benchmarks.get("tiers", BENCHMARKS_DEFAULT["tiers"]).get(category, {})
    fator = tiers_data.get(tier, {}).get("fator_novo", 0.55)

    return {
        "novo_loja": round(base["novo_loja"] * fator),
        "novo_ml":   round(base["novo_ml"]   * fator),
        "tier":      tier,
        "bench_key": grupo_tier,
    }

# ──────────────────────────────────────────────────────────────
# SCORE PRINCIPAL — 100 PONTOS
# ──────────────────────────────────────────────────────────────

def score_speed(attrs: dict, benchmarks: dict) -> tuple[int, dict]:
    """Retorna (score_total, breakdown_dict)."""
    bd = {}

    # 1. Preço vs. usados (30 pts)
    preco  = attrs.get("price_int")
    grupo  = attrs.get("grupo", "")
    mat    = attrs.get("material", "aluminio")
    bench_key = "alu_105"
    if "di2" in norm(grupo) or "etap" in norm(grupo):
        bench_key = "carbono_di2" if "carbono" in mat else "alu_ultegra"
    elif any(k in norm(grupo) for k in ["ultegra","force","red"]):
        bench_key = "carbono_ultegra" if "carbono" in mat else "alu_ultegra"
    elif any(k in norm(grupo) for k in ["rival"]):
        bench_key = "carbono_105" if "carbono" in mat else "alu_rival"
    elif "carbono" in mat:
        bench_key = "carbono_105"

    cat_bench = benchmarks.get("speed", BENCHMARKS_DEFAULT["speed"])
    ref = cat_bench.get(bench_key, cat_bench["alu_105"])
    median = ref["median"]
    attrs["bench_key"] = bench_key
    attrs["bench_median"] = median

    if preco and median:
        pct_off = (median - preco) / median
        if   pct_off >= 0.30: bd["preco"] = 30
        elif pct_off >= 0.20: bd["preco"] = 22
        elif pct_off >= 0.10: bd["preco"] = 14
        elif pct_off >= 0.05: bd["preco"] = 10
        elif pct_off >= 0.00: bd["preco"] = 7
        else:                 bd["preco"] = 0
    else:
        bd["preco"] = 5  # sem preço declarado: pontuação mínima

    # 2. Grupo (25 pts)
    g = norm(grupo)
    if any(k in g for k in ["dura-ace di2","ultegra di2","105 di2","red etap","force etap"]):
        bd["grupo"] = 25
    elif any(k in g for k in ["ultegra r8100","ultegra r8000","ultegra","force","red"]):
        bd["grupo"] = 20
    elif any(k in g for k in ["105 r7100","rival etap"]):
        bd["grupo"] = 18
    elif "105 r7000" in g:
        bd["grupo"] = 14
    elif "105 5800" in g or "105" in g:
        bd["grupo"] = 10
    elif "rival" in g:
        bd["grupo"] = 12
    else:
        bd["grupo"] = 0

    # 3. Material quadro (20 pts)
    if "carbono_himod" in mat:  bd["material"] = 20
    elif "carbono" in mat:      bd["material"] = 17
    else:                       bd["material"] = 10

    # 4. Rodas (15 pts)
    rodas = norm(attrs.get("rodas") or "")
    if any(k in rodas for k in ["zipp","enve","roval carbon","dt swiss xrc","carbon"]):
        bd["rodas"] = 15
    elif any(k in rodas for k in ["ksyrium","dt swiss","fulcrum","vision","paradigm elite"]):
        bd["rodas"] = 10
    else:
        bd["rodas"] = 5

    # 5. Peso (10 pts)
    peso = attrs.get("weight")
    if   peso and peso < 7.5:  bd["peso"] = 10
    elif peso and peso < 8.5:  bd["peso"] = 7
    elif peso and peso < 9.5:  bd["peso"] = 4
    else:                      bd["peso"] = 1

    total = sum(bd.values())
    return min(total, 100), bd


def score_mtb(attrs: dict, benchmarks: dict) -> tuple[int, dict]:
    bd = {}

    # 1. Preço vs. usados (30 pts)
    preco  = attrs.get("price_int")
    grupo  = attrs.get("grupo", "")
    mat    = attrs.get("material", "aluminio")
    susp   = attrs.get("suspensao") or ""
    g = norm(grupo)

    bench_key = "alu_slx_rockshox"
    if any(k in g for k in ["xtr","xx1","x01"]):
        bench_key = "carbono_xtr_eagle"
    elif any(k in g for k in ["xt","gx eagle"]):
        bench_key = "carbono_xt_fox" if "carbono" in mat else "alu_xt_fox"
    elif any(k in g for k in ["slx","nx eagle","gx"]):
        bench_key = "carbono_slx" if "carbono" in mat else "alu_slx_rockshox"

    cat_bench = benchmarks.get("mtb", BENCHMARKS_DEFAULT["mtb"])
    ref = cat_bench.get(bench_key, cat_bench["alu_slx_rockshox"])
    median = ref["median"]
    attrs["bench_key"] = bench_key
    attrs["bench_median"] = median

    if preco and median:
        pct_off = (median - preco) / median
        if   pct_off >= 0.30: bd["preco"] = 30
        elif pct_off >= 0.20: bd["preco"] = 22
        elif pct_off >= 0.10: bd["preco"] = 14
        elif pct_off >= 0.05: bd["preco"] = 10
        elif pct_off >= 0.00: bd["preco"] = 7
        else:                 bd["preco"] = 0
    else:
        bd["preco"] = 5

    # 2. Grupo (25 pts)
    if any(k in g for k in ["xtr","xx1 axs","xx1 eagle"]):    bd["grupo"] = 25
    elif any(k in g for k in ["x01","xt m8100","xt m8000"]):   bd["grupo"] = 20
    elif any(k in g for k in ["slx m7100","slx","gx eagle"]):  bd["grupo"] = 15
    elif any(k in g for k in ["deore m6100","nx eagle","nx"]):  bd["grupo"] = 10
    else:                                                        bd["grupo"] = 0

    # 3. Suspensão (20 pts)
    s = norm(susp)
    if any(k in s for k in ["fox 36 factory","fox 34 factory","fox factory","rockshox lyrik ulti"]):
        bd["suspensao"] = 20
    elif any(k in s for k in ["fox 36 rhythm","fox 34 rhythm","fox rhythm","rockshox pike ulti","rockshox pike select","rockshox sid sl"]):
        bd["suspensao"] = 17
    elif any(k in s for k in ["rockshox pike","rockshox sid","rockshox recon rl"]):
        bd["suspensao"] = 12
    elif any(k in s for k in ["sr suntour","suntour","recon gold"]):
        bd["suspensao"] = 4
    else:
        bd["suspensao"] = 6  # não identificada

    # 4. Material quadro (15 pts)
    if "carbono_himod" in mat:  bd["material"] = 15
    elif "carbono" in mat:      bd["material"] = 15
    else:                       bd["material"] = 8

    # 5. Canote retrátil (10 pts)
    tem_canote, curso = detect_canote(attrs.get("title","") + " " + attrs.get("desc",""))
    if   tem_canote and curso >= 150: bd["canote"] = 10
    elif tem_canote:                   bd["canote"] = 6
    else:                              bd["canote"] = 0

    total = sum(bd.values())
    return min(total, 100), bd


# ──────────────────────────────────────────────────────────────
# NOTA DE VALOR PERCEBIDO — 100 PONTOS
# ──────────────────────────────────────────────────────────────

def calc_vp(attrs: dict, category: str, benchmarks: dict) -> tuple[int, dict]:
    preco  = attrs.get("price_int")
    grupo  = attrs.get("grupo", "")
    mat    = attrs.get("material", "aluminio")
    brand  = attrs.get("brand")

    bench_key = attrs.get("bench_key", "")
    ref_novo  = get_novo_reference(brand, category, mat, bench_key, benchmarks)
    novo_loja = ref_novo["novo_loja"]
    novo_ml   = ref_novo["novo_ml"]
    tier      = ref_novo["tier"]

    # Eixo 1 — desconto vs. novo loja (50 pts)
    bd = {"tier": tier, "novo_loja": novo_loja, "novo_ml": novo_ml}
    if preco and novo_loja:
        pct = (novo_loja - preco) / novo_loja
        if   pct >= 0.65: bd["desconto_novo"] = 50
        elif pct >= 0.55: bd["desconto_novo"] = 42
        elif pct >= 0.45: bd["desconto_novo"] = 34
        elif pct >= 0.35: bd["desconto_novo"] = 24
        elif pct >= 0.25: bd["desconto_novo"] = 14
        else:             bd["desconto_novo"] = 5
        bd["pct_desconto_loja"] = round(pct * 100)
        bd["economia_loja"]     = novo_loja - preco
    else:
        bd["desconto_novo"] = 10
        bd["pct_desconto_loja"] = None
        bd["economia_loja"]     = None

    # Eixo 2 — spec vs. equivalente novo (50 pts)
    g = norm(grupo)
    if category == "speed":
        if any(k in g for k in ["di2","etap"]) and "carbono" in mat:  bd["spec"] = 50
        elif any(k in g for k in ["ultegra","force","red"]):           bd["spec"] = 40
        elif "105 r7100" in g or "105 r7000" in g:                     bd["spec"] = 32
        elif "105" in g and "carbono" in mat:                          bd["spec"] = 28
        elif "105" in g:                                                bd["spec"] = 20
        elif "rival" in g:                                              bd["spec"] = 22
        else:                                                           bd["spec"] = 10
    else:  # mtb
        if any(k in g for k in ["xtr","xx1"]):                             bd["spec"] = 50
        elif any(k in g for k in ["x01","xt m8100"]) and "carbono" in mat: bd["spec"] = 44
        elif any(k in g for k in ["xt","x01"]):                            bd["spec"] = 38
        elif any(k in g for k in ["slx","gx eagle"]) and "carbono" in mat: bd["spec"] = 30
        elif any(k in g for k in ["slx","gx eagle"]):                      bd["spec"] = 24
        elif any(k in g for k in ["deore m6100","nx eagle"]):              bd["spec"] = 16
        else:                                                               bd["spec"] = 8

    vp_total = bd["desconto_novo"] + bd["spec"]
    return min(vp_total, 100), bd


# ──────────────────────────────────────────────────────────────
# FILTROS ELIMINATÓRIOS
# ──────────────────────────────────────────────────────────────

def passes_filters(attrs: dict, category: str) -> tuple[bool, str]:
    text = norm(attrs.get("title","") + " " + attrs.get("desc",""))

    if text_contains_any(text, KEYWORDS_DESCARTAR):
        return False, "categoria indesejada"

    if not price_ok(attrs.get("price_int")):
        return False, f"preço acima de R${CONFIG['preco_max']:,}"

    if category == "speed":
        grupo = norm(attrs.get("grupo") or "")
        if not grupo:
            # sem grupo identificado: não elimina, deixa o score julgar
            pass
        elif text_contains_any(grupo, FILTROS_SPEED["grupos_nok"]):
            return False, f"grupo insuficiente: {grupo}"

        ano = attrs.get("year")
        if ano:
            mat = attrs.get("material", "aluminio")
            ano_min = FILTROS_SPEED["ano_min_carb"] if "carbono" in mat else FILTROS_SPEED["ano_min_alu"]
            if ano < ano_min:
                return False, f"ano muito antigo: {ano}"

        tam = attrs.get("size")
        if tam and tam not in ["s","m","l","xs","xl"]:
            try:
                t_int = int(str(tam))
                # Elimina apenas tamanhos claramente incompatíveis (<47 ou >62)
                if t_int < 47 or t_int > 62:
                    return False, f"tamanho fora do range: {tam}"
            except ValueError:
                pass

    elif category == "mtb":
        grupo = norm(attrs.get("grupo") or "")
        if grupo and text_contains_any(grupo, FILTROS_MTB["grupos_nok"]):
            return False, f"grupo insuficiente: {grupo}"

        # Aro obrigatório 29
        text_full = norm(attrs.get("title","") + " " + attrs.get("desc",""))
        if re.search(r"\baro\s*27", text_full) and "mullet" not in text_full:
            return False, "aro 27.5 sem mullet"

    return True, ""


# ──────────────────────────────────────────────────────────────
# ENRIQUECIMENTO COMPLETO DE UM ANÚNCIO
# ──────────────────────────────────────────────────────────────

def enrich(listing: dict, benchmarks: dict, bike_db: dict | None = None) -> dict | None:
    """
    Recebe um anúncio bruto, extrai atributos, aplica filtros e calcula scores.
    Enriquece com database de modelos se bike_db fornecido.
    Retorna None se eliminado, ou o dict enriquecido com score + vp.
    """
    title = listing.get("title", "")
    desc  = listing.get("desc",  "")
    text  = title + " " + desc

    category = detect_category(title, desc)
    if not category:
        return None

    brand    = detect_brand(text, category)
    material = detect_material(text)
    size     = extract_size(text)
    weight   = extract_weight(text)
    year     = extract_year(text)
    nf       = detect_nf(text)

    if category == "speed":
        grupo = detect_grupo_speed(text)
        rodas = detect_rodas(text)
        attrs = {**listing, "category": category, "brand": brand, "material": material,
                 "grupo": grupo or "", "rodas": rodas, "size": size, "weight": weight,
                 "year": year, "nf": nf, "desc": desc}
    else:
        grupo = detect_grupo_mtb(text)
        susp  = detect_suspensao(text)
        attrs = {**listing, "category": category, "brand": brand, "material": material,
                 "grupo": grupo or "", "suspensao": susp, "size": size, "weight": weight,
                 "year": year, "nf": nf, "desc": desc}

    # ── Enriquece com database de modelos ──────────────────────
    if bike_db:
        try:
            attrs = enrich_from_db(title, desc, attrs, bike_db)
            if attrs.get("db_match"):
                log.debug(f"DB match: {attrs.get('db_model_name')} {attrs.get('db_year_used')} "
                          f"→ grupo: {attrs.get('grupo')} ({attrs.get('grupo_source')})")
            elif attrs.get("grupo_nao_confirmado"):
                log.debug(f"DB sem match, sem grupo: '{title[:50]}' → alerta ativado")
        except Exception as e:
            log.warning(f"db_matcher falhou para '{title[:50]}': {e}")
    # ──────────────────────────────────────────────────────────

    ok, reason = passes_filters(attrs, category)
    if not ok:
        log.debug(f"Eliminado ({reason}): {title[:60]}")
        return None

    # Scores
    if category == "speed":
        score, score_bd = score_speed(attrs, benchmarks)
    else:
        score, score_bd = score_mtb(attrs, benchmarks)

    vp, vp_bd = calc_vp(attrs, category, benchmarks)

    attrs["score"]    = score
    attrs["score_bd"] = score_bd
    attrs["vp"]       = vp
    attrs["vp_bd"]    = vp_bd

    return attrs


# ──────────────────────────────────────────────────────────────
# SCRAPERS
# ──────────────────────────────────────────────────────────────

BUSCA_OLX = [
    "cannondale CAAD10","cannondale CAAD13","cannondale supersix",
    "trek emonda","giant TCR","specialized tarmac",
    "bike speed carbono 105","bike speed ultegra",
    "MTB 29 XT 12v","MTB 29 carbono SLX","stumpjumper 29","sense exper carbono",
]

BAZARBIKES_ENDPOINTS = [
    {"url": "https://bazarbikes.com.br/collections/bikes-speed/products.json?limit=250",     "cat": "speed"},
    {"url": "https://bazarbikes.com.br/collections/mountain-bike/products.json?limit=250",   "cat": "mtb"},
    {"url": "https://bazarbikes.com.br/collections/bicicletas/products.json?limit=250",      "cat": "all"},
]

SEMEXE_URLS = [
    "https://www.semexe.com/bikes/estrada/",
    "https://www.semexe.com/bikes/mtb/",
]


def scrape_bazarbikes(endpoints: list) -> list:
    results = []
    for ep in endpoints:
        log.info(f"BazarBikes: {ep['cat']}")
        r = get_proxied(ep["url"])
        if not r:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        for product in data.get("products", []):
            title  = product.get("title", "")
            handle = product.get("handle", "")
            images = product.get("images", [])
            img    = images[0].get("src", "") if images else ""
            body   = BeautifulSoup(product.get("body_html",""), "html.parser").get_text(" ")
            for variant in product.get("variants", []):
                price_s   = variant.get("price", "0")
                price_int = int(float(price_s)) if price_s else None
                var_id    = str(variant.get("id",""))
                prod_id   = str(product.get("id",""))
                results.append({
                    "id":        make_id("bazarbikes", f"{prod_id}_{var_id}"),
                    "source":    "BazarBikes",
                    "title":     f"{title} — {variant.get('title','')}".strip(" —"),
                    "desc":      body[:400],
                    "price":     f"R$ {price_int:,}".replace(",",".") if price_int else "Consultar",
                    "price_int": price_int,
                    "url":       f"https://bazarbikes.com.br/products/{handle}",
                    "img":       img,
                    "city":      "São Paulo",
                })
        time.sleep(CONFIG["delay"])
    return results


def _semexe_product_links(listing_url: str) -> list[str]:
    """Extrai links de produto de uma página de listagem Semexe, com paginação."""
    links = set()
    page  = 1
    while True:
        url = f"{listing_url}?page={page}" if page > 1 else listing_url
        r   = get(url, headers=HEADERS_SEMEXE)
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        # CS-Cart: links de produto ficam em <a> com href contendo o slug do produto
        found = 0
        for a in soup.select("a[href]"):
            href = a["href"]
            # URL de produto individual: /bikes/estrada/nome-do-produto/ ou /bikes/mtb/...
            if re.match(r"https?://www[.]semexe[.]com/bikes/[^/]+/[^/]+/$", href):
                if href not in links:
                    links.add(href)
                    found += 1
            elif re.match(r"^/bikes/[^/]+/[^/]+/$", href):
                full = "https://www.semexe.com" + href
                if full not in links:
                    links.add(full)
                    found += 1
        # Sem novos links nessa página = acabou a paginação
        if found == 0:
            break
        # Verifica se existe "próxima página"
        next_pg = soup.select_one("a.ty-pagination__next, a[rel='next'], .ty-pagination li.last a")
        if not next_pg:
            break
        page += 1
        if page > 10:  # teto de segurança: máx 10 páginas por categoria
            break
        time.sleep(CONFIG["delay"])
    return list(links)


def _semexe_parse_product(url: str) -> dict | None:
    """
    Lê uma página de produto Semexe e extrai dados via meta tags OpenGraph.
    Muito mais confiável que parsear o HTML da grade.
    """
    r = get(url, headers=HEADERS_SEMEXE)
    if not r:
        return None
    soup  = BeautifulSoup(r.text, "html.parser")

    def meta(prop: str) -> str:
        tag = (soup.find("meta", property=prop) or
               soup.find("meta", attrs={"name": prop}))
        return tag["content"].strip() if tag and tag.get("content") else ""

    title     = meta("og:title").replace("Semexe - ", "").strip()
    price_s   = meta("product:price:amount")
    currency  = meta("product:price:currency")
    condition = meta("product:condition")   # "used" / "new"
    desc      = meta("og:description")
    img       = meta("og:image")
    prod_id   = meta("product:retailer_item_id") or hashlib.md5(url.encode()).hexdigest()[:12]

    if not title:
        return None

    price_int = int(float(price_s)) if price_s else None
    price_fmt = f"R$ {price_int:,}".replace(",", ".") if price_int else "Consultar"

    return {
        "id":        make_id("semexe", prod_id),
        "source":    "Semexe",
        "title":     title,
        "desc":      desc[:400],
        "price":     price_fmt,
        "price_int": price_int,
        "url":       url,
        "img":       img,
        "city":      "São Paulo",
        "condition": condition,
    }


def scrape_semexe(urls: list) -> list:
    """
    Estratégia:
    1. Varre cada URL de listagem para coletar links de produtos individuais
    2. Para cada produto, lê as meta tags OpenGraph (dados estruturados, confiáveis)
    """
    results = []
    all_links: set = set()

    for listing_url in urls:
        log.info(f"Semexe listagem: {listing_url}")
        links = _semexe_product_links(listing_url)
        log.info(f"  {len(links)} links de produto encontrados")
        all_links.update(links)
        time.sleep(CONFIG["delay"])

    log.info(f"Semexe: {len(all_links)} produtos únicos para processar")
    for url in all_links:
        product = _semexe_parse_product(url)
        if product:
            results.append(product)
        time.sleep(CONFIG["delay"])

    log.info(f"Semexe: {len(results)} produtos coletados")
    return results


# Queries de busca para o Firecrawl Search — Bikemagazine Classificados
# Estratégia: busca indexada em vez de scraping direto (contorna bloqueio)
BIKEMAGAZINE_QUERIES = [
    "site:classificados.bikemagazine.com.br bikes completas speed carbono 105",
    "site:classificados.bikemagazine.com.br bikes completas speed ultegra",
    "site:classificados.bikemagazine.com.br bikes completas speed cannondale trek specialized",
    "site:classificados.bikemagazine.com.br bikes completas mountain bike XT 29",
    "site:classificados.bikemagazine.com.br bikes completas mountain bike carbono 29",
]


def firecrawl_search(query: str, limit: int = 5) -> list[dict]:
    """
    Usa Firecrawl /search para buscar anúncios no Bikemagazine via índice web.
    Retorna lista de resultados com url, title, description, markdown.
    Consome 1 crédito por resultado (limit=5 → 5 créditos por query).
    """
    if not FIRECRAWL_API_KEY:
        return []
    try:
        resp = requests.post(
            f"{FIRECRAWL_BASE}/search",
            headers={
                "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={"query": query, "limit": limit},
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning(f"Firecrawl Search HTTP {resp.status_code} para '{query[:50]}'")
            return []
        data = resp.json()
        if not data.get("success"):
            log.warning(f"Firecrawl Search erro: {data.get('error','?')}")
            return []
        return data.get("data", [])
    except Exception as e:
        log.warning(f"Firecrawl Search exceção: {e}")
        return []


def scrape_bikemagazine(queries: list = None) -> list:
    """
    Busca anúncios do Bikemagazine via Firecrawl Search (índice web).
    Não acessa o site diretamente — contorna bloqueio de datacenter.
    Cada resultado já vem com título, descrição e markdown do conteúdo.
    """
    if queries is None:
        queries = BIKEMAGAZINE_QUERIES

    if not FIRECRAWL_API_KEY:
        log.warning("Bikemagazine: FIRECRAWL_API_KEY não configurada — pulando")
        return []

    results = []
    seen_urls: set = set()

    for query in queries:
        log.info(f"Bikemagazine search: '{query[:60]}'")
        hits = firecrawl_search(query, limit=5)
        log.info(f"  {len(hits)} resultados")

        for hit in hits:
            url = hit.get("url", "")

            # Filtra apenas URLs de anúncios individuais do Bikemagazine
            if not re.match(r"https?://classificados\.bikemagazine\.com\.br/anuncios/[0-9]", url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title    = (hit.get("title") or "").strip()
            desc_raw = (hit.get("description") or "").strip()
            markdown = (hit.get("markdown") or "")

            # Limpa título — Bikemagazine adiciona "Classificados Bikemagazine - " no início
            title = re.sub(r"^Classificados Bikemagazine\s*[-–]\s*", "", title).strip()

            if not title:
                continue

            # Extrai preço do markdown (mais rico que a description)
            price_s   = ""
            price_int = None
            m = re.search(r"R\$\s*([\d.,]+)", markdown or desc_raw)
            if m:
                price_s   = "R$ " + m.group(1)
                price_int = parse_price(price_s)

            # ID do anúncio a partir da URL
            ad_id_m = re.search(r"/anuncios/([0-9]+)", url)
            ad_id   = ad_id_m.group(1) if ad_id_m else hashlib.md5(url.encode()).hexdigest()[:10]

            # Cidade: busca no markdown
            city = ""
            city_m = re.search(r"Cidade[^\n]*\n([^\n]+)", markdown)
            if city_m:
                city = city_m.group(1).strip()

            results.append({
                "id":        make_id("bikemagazine", ad_id),
                "source":    "Bikemagazine",
                "title":     title,
                "desc":      (markdown[:600] if markdown else desc_raw[:400]),
                "price":     price_s or "A combinar",
                "price_int": price_int,
                "url":       url,
                "city":      city,
            })

        time.sleep(CONFIG["delay"])

    log.info(f"Bikemagazine: {len(results)} anúncios únicos coletados")
    return results


def scrape_olx(queries: list) -> list:
    results = []
    base = "https://www.olx.com.br/brasil/esportes-e-lazer/bicicletas"
    for query in queries:
        url = f"{base}?q={requests.utils.quote(query)}&sf=1"
        log.info(f"OLX: '{query}'")
        r = get_proxied(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        ads = []
        if script:
            try:
                data = json.loads(script.string)
                ads  = data.get("props",{}).get("pageProps",{}).get("ads",[])
            except Exception:
                pass
        if not ads:
            # fallback HTML
            for card in soup.select("li[data-lurker-detail='list_id']"):
                t_el = card.select_one("h2, [class*='title']")
                p_el = card.select_one("[class*='price']")
                a_el = card.select_one("a[href]")
                if not t_el or not a_el:
                    continue
                url_ad = a_el["href"]
                ads.append({
                    "subject":  t_el.get_text(strip=True),
                    "price":    p_el.get_text(strip=True) if p_el else "",
                    "url":      url_ad,
                    "listId":   hashlib.md5(url_ad.encode()).hexdigest()[:10],
                    "location": {},
                })
        for ad in ads:
            title   = ad.get("subject","")
            price_s = ad.get("price","")
            url_ad  = ad.get("url","")
            loc     = ad.get("location",{})
            city    = f"{loc.get('municipality','')}-{loc.get('uf','')}".strip("-")
            ad_id   = str(ad.get("listId", hashlib.md5(url_ad.encode()).hexdigest()[:10]))
            price_int = parse_price(price_s)
            results.append({
                "id":        make_id("olx", ad_id),
                "source":    "OLX",
                "title":     title,
                "desc":      "",
                "price":     price_s or "A combinar",
                "price_int": price_int,
                "url":       url_ad,
                "city":      city,
            })
        time.sleep(CONFIG["delay"])
    return results



# ──────────────────────────────────────────────────────────────
# SCORE CONDICIONAL — preço negociado (10% e 20% de desconto)
# ──────────────────────────────────────────────────────────────

def calc_score_negociado(enriched: dict, benchmarks: dict, desconto_pct: float) -> tuple[int, int]:
    """
    Recalcula score e VP com preço negociado (desconto_pct = 0.10 ou 0.20).
    Retorna (score_negociado, vp_negociado).
    Mantém todos os outros atributos iguais — só o preço muda.
    """
    preco_orig = enriched.get("price_int")
    if not preco_orig:
        return enriched.get("score", 0), enriched.get("vp", 0)

    preco_neg = round(preco_orig * (1 - desconto_pct))
    category  = enriched.get("category", "speed")

    # Cria cópia com preço negociado
    mock = dict(enriched)
    mock["price_int"] = preco_neg
    mock["price"]     = f"R$ {preco_neg:,}".replace(",",".")

    if category == "speed":
        sc, _ = score_speed(mock, benchmarks)
    else:
        sc, _ = score_mtb(mock, benchmarks)

    _, vp_bd = calc_vp(mock, category, benchmarks)
    vp = min(vp_bd["desconto_novo"] + vp_bd["spec"], 100)

    return sc, vp


def is_oportunidade_condicional(enriched: dict, benchmarks: dict) -> dict | None:
    """
    Verifica se o anúncio NÃO passa no score real mas PASSARIA com 10% ou 20% de desconto.
    Retorna dict com detalhes dos dois cenários, ou None se não for condicional.
    """
    sc_real = enriched.get("score", 0)
    vp_real = enriched.get("vp",    0)
    score_min = CONFIG["score_min"]
    vp_min    = CONFIG["vp_min"]

    # Já passa no real → não é condicional
    if sc_real >= score_min and vp_real >= vp_min:
        return None

    preco_orig = enriched.get("price_int")
    if not preco_orig:
        return None

    sc_10, vp_10 = calc_score_negociado(enriched, benchmarks, 0.10)
    sc_20, vp_20 = calc_score_negociado(enriched, benchmarks, 0.20)

    passa_10 = sc_10 >= score_min and vp_10 >= vp_min
    passa_20 = sc_20 >= score_min and vp_20 >= vp_min

    if not passa_10 and not passa_20:
        return None

    return {
        "preco_orig":  preco_orig,
        "preco_10":    round(preco_orig * 0.90),
        "preco_20":    round(preco_orig * 0.80),
        "sc_10":       sc_10,  "vp_10": vp_10,  "passa_10": passa_10,
        "sc_20":       sc_20,  "vp_20": vp_20,  "passa_20": passa_20,
        "sc_real":     sc_real, "vp_real": vp_real,
    }

# ──────────────────────────────────────────────────────────────
# ANÁLISE CLAUDE (opcional)
# ──────────────────────────────────────────────────────────────

def analyze_with_claude(listing: dict) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY","")
    if not api_key:
        return ""
    import urllib.request, urllib.error

    cat  = listing.get("category","")
    brand = listing.get("brand","")
    tier  = listing.get("vp_bd",{}).get("tier","?")
    novo  = listing.get("vp_bd",{}).get("novo_loja",0)
    eco   = listing.get("vp_bd",{}).get("economia_loja",0)
    pct   = listing.get("vp_bd",{}).get("pct_desconto_loja","?")
    score = listing.get("score",0)
    vp    = listing.get("vp",0)

    prompt = f"""Sou ciclista em São Paulo (70kg, 173cm). Tenho CAAD10 speed (105 5800 2x11v) e Sense React Sport MTB (2x8v).
Encontrei este anúncio. Analise em 3–4 frases diretas, sem elogios genéricos:

Anúncio: {listing['title']}
Descrição: {listing.get('desc','')[:300]}
Preço: {listing['price']} | Score: {score}/100 | VP: {vp}/100
Tier da marca: {tier} | Novo loja: R${novo:,} | Economia: R${eco:,} | Desconto: {pct}%
Categoria: {cat}

Foque em: (1) se é oportunidade real dado o preço vs. mercado, (2) principal downgrade vs. novo equivalente, 
(3) um ponto de atenção prático antes de contatar o vendedor.
Máximo 80 palavras. Português. Sem markdown."""

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 200,
        "messages": [{"role":"user","content":prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"].strip()
    except Exception as e:
        log.warning(f"Claude API falhou: {e}")
        return ""


# ──────────────────────────────────────────────────────────────
# E-MAIL HTML
# ──────────────────────────────────────────────────────────────

SOURCE_COLORS = {
    "OLX":       ("#fff0eb", "#7a2e00"),
    "BazarBikes":("#e1f5ee", "#085041"),
    "Semexe":    ("#e6f1fb", "#0c447c"),
}
TIER_COLORS = {
    "A": ("#e6f1fb","#0c447c"),
    "B": ("#eaf3de","#27500a"),
    "C": ("#faeeda","#633806"),
}
CAT_COLORS = {
    "speed": ("#eeedfe","#3c3489"),
    "mtb":   ("#eaf3de","#27500a"),
}

def score_color(s: int) -> str:
    if s >= 80: return "#0b7a6e"
    if s >= 65: return "#3b6d11"
    if s >= 50: return "#854f0b"
    return "#a32d2d"

def chip(label: str, bg: str, color: str) -> str:
    return (f'<span style="display:inline-block;font-size:9px;padding:2px 6px;'
            f'border-radius:3px;font-family:monospace;font-weight:500;'
            f'background:{bg};color:{color}">{label}</span>')

def bar_row(label: str, pts: int, max_pts: int, color: str) -> str:
    pct = round(pts / max_pts * 100) if max_pts else 0
    return f"""
    <tr>
      <td style="font-size:10px;color:#6a6960;width:120px;padding:3px 0">{label}</td>
      <td style="padding:3px 6px">
        <div style="background:#f0eeea;border-radius:3px;height:5px;overflow:hidden">
          <div style="width:{pct}%;height:5px;background:{color};border-radius:3px"></div>
        </div>
      </td>
      <td style="font-size:10px;font-weight:600;color:{color};font-family:monospace;white-space:nowrap;padding:3px 0">{pts}/{max_pts}</td>
    </tr>"""

def render_listing_html(l: dict, rank: int, analysis: str) -> str:
    src_bg,  src_fg  = SOURCE_COLORS.get(l["source"], ("#f0eeea","#444"))
    tier             = l.get("vp_bd",{}).get("tier","C")
    tier_bg, tier_fg = TIER_COLORS.get(tier, TIER_COLORS["C"])
    cat              = l.get("category","speed")
    cat_bg,  cat_fg  = CAT_COLORS.get(cat, CAT_COLORS["speed"])
    sc               = l.get("score", 0)
    vp               = l.get("vp", 0)
    sc_col           = score_color(sc)
    vp_col           = score_color(vp)
    urgent           = sc >= 75 and vp >= 75
    border           = "1.5px solid #0b7a6e" if urgent else "1px solid #edecea"
    novo_loja        = l.get("vp_bd",{}).get("novo_loja", 0)
    novo_ml          = l.get("vp_bd",{}).get("novo_ml", 0)
    median           = l.get("bench_median", 0)
    eco              = l.get("vp_bd",{}).get("economia_loja", 0)
    pct_loja         = l.get("vp_bd",{}).get("pct_desconto_loja","?")
    pct_used         = round((median - l["price_int"]) / median * 100) if (median and l.get("price_int")) else "?"
    mat_label        = "carbono hi-mod" if "himod" in (l.get("material","")) else l.get("material","")
    grupo_src        = l.get("grupo_source","")
    grupo_raw        = l.get("grupo","—")
    grupo_label      = f"{grupo_raw} ({'título' if grupo_src=='titulo' else 'DB'})" if grupo_src and grupo_src != "nenhum" else grupo_raw
    susp_label       = l.get("suspensao","—") if cat == "mtb" else None
    size_label       = l.get("size","—")
    year_label       = str(l.get("year","")) if l.get("year") else ""
    nf_label         = "NF" if l.get("nf") else ""
    db_tag           = f"DB {l.get('db_year_used','')}" if l.get("db_match") else ""
    alerta_grupo     = "⚠ grupo não confirmado — verificar no anúncio" if l.get("grupo_nao_confirmado") else ""
    sub_parts        = [p for p in [mat_label, f"Tam {size_label}", grupo_label,
                                     susp_label, year_label, l.get("city",""), nf_label, db_tag] if p and p != "—"]
    subtitle         = " · ".join(sub_parts[:7])
    subtitle        += f'<br><span style="font-size:10px;color:#c47c0a;font-weight:500">{alerta_grupo}</span>' if alerta_grupo else ""

    # Score breakdown bars
    bd    = l.get("score_bd", {})
    bars  = ""
    max_map = {"preco":30,"grupo":25,"material":20,"rodas":15,"peso":10,
                "suspensao":20,"canote":10}
    labels  = {"preco":"Preço vs. usados","grupo":"Grupo","material":"Quadro",
                "rodas":"Rodas","peso":"Peso","suspensao":"Suspensão","canote":"Canote retrátil"}
    for k, pts in bd.items():
        if k in max_map:
            bars += bar_row(labels.get(k,k), pts, max_map[k], sc_col)

    urgent_badge = chip("urgente","#e1f5ee","#085041") + " " if urgent else ""
    ins_bg   = "#f4f9f7" if not urgent else "#f0faf7"
    ins_border = "#0b7a6e"

    return f"""
<div style="margin:0 16px 12px;border:{border};border-radius:7px;overflow:hidden">
  <div style="padding:10px 13px 8px">
    <div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px;align-items:center">
      <span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;font-size:9px;font-weight:700;font-family:monospace;background:{'#0b7a6e' if urgent else '#eaf3de'};color:{'white' if urgent else '#27500a'}">{rank}</span>
      {urgent_badge}{chip(l['source'],src_bg,src_fg)} {chip('Tier '+tier,tier_bg,tier_fg)} {chip(cat,cat_bg,cat_fg)} {''+chip('carbono','#f1eefe','#534ab7') if 'carbono' in (l.get('material','')) else chip('alumínio','#f1f0ea','#5f5e5a')}
    </div>
    <div style="font-size:13px;font-weight:700;color:#1c1b18;letter-spacing:-.01em;line-height:1.3;margin-bottom:2px">{l['title']}</div>
    <div style="font-size:11px;color:#6a6960">{subtitle}</div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;border-top:1px solid #f0eeea;border-bottom:1px solid #f0eeea">
    <div style="padding:8px 13px;border-right:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Preço pedido</div>
      <div style="font-size:14px;font-weight:700;color:#0b7a6e">{l['price']}</div>
    </div>
    <div style="padding:8px 13px">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Mediana usados similares</div>
      <div style="font-size:14px;font-weight:600;color:#1c1b18">{f"R$ {median:,}".replace(",",".") if median else "—"}</div>
    </div>
    <div style="padding:8px 13px;border-right:1px solid #f0eeea;border-top:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Novo loja (tier {tier})</div>
      <div style="font-size:13px;font-weight:600;color:#1c1b18">{f"R$ {novo_loja:,}".replace(",",".") if novo_loja else "—"}</div>
    </div>
    <div style="padding:8px 13px;border-top:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Novo ML (melhor preço)</div>
      <div style="font-size:13px;font-weight:600;color:#1c1b18">{f"R$ {novo_ml:,}".replace(",",".") if novo_ml else "—"}</div>
    </div>
  </div>
  <div style="padding:8px 13px;border-bottom:1px solid #f0eeea">
    <table style="width:100%;border-collapse:collapse">{"".join(bars)}</table>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 13px;gap:8px;flex-wrap:wrap">
    <div>
      <div style="font-size:20px;font-weight:700;color:#0b7a6e;letter-spacing:-.02em">{l['price']}</div>
      <div style="font-size:10px;color:#9b9a94;font-family:monospace">−{pct_used}% usados · −{pct_loja}% loja nova</div>
    </div>
    <div style="display:flex;gap:14px;align-items:center">
      <div style="text-align:center">
        <div style="font-size:20px;font-weight:700;color:{sc_col}">{sc}</div>
        <div style="font-size:9px;color:#9b9a94;font-family:monospace">score</div>
      </div>
      <div style="color:#e0e0e0">|</div>
      <div style="text-align:center">
        <div style="font-size:20px;font-weight:700;color:{vp_col}">{vp}</div>
        <div style="font-size:9px;color:#9b9a94;font-family:monospace">VP</div>
      </div>
      {"<div style='color:#e0e0e0'>|</div><div style='font-size:10px;color:#6a6960;text-align:right'><div style='font-weight:600;color:#1c1b18'>economia R$ "+str(f'{eco:,}').replace(',','.')+"</div><div>vs. loja nova</div></div>" if eco else ""}
    </div>
    <a href="{l['url']}" style="display:inline-block;background:#1c1b18;color:#f4f3ef;font-size:11px;font-weight:700;padding:8px 14px;border-radius:5px;text-decoration:none">Ver em {l['source']} →</a>
  </div>
</div>
{"" if not analysis else f'<div style="margin:0 16px 12px;background:{ins_bg};border-left:2px solid {ins_border};padding:9px 13px"><div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#085041;font-family:monospace;margin-bottom:3px;font-weight:500">análise claude</div><div style="font-size:11px;color:#1c1b18;line-height:1.6">{analysis}</div></div>'}
<div style="height:1px;background:#f0eeea;margin:0 16px 6px"></div>"""



def render_condicional_html(l: dict, rank: int, cond: dict) -> str:
    """Card HTML para oportunidade condicional — compacto, badge laranja."""
    cat              = l.get("category","speed")
    cat_bg, cat_fg   = CAT_COLORS.get(cat, CAT_COLORS["speed"])
    src_bg, src_fg   = SOURCE_COLORS.get(l["source"], ("#f0eeea","#444"))
    tier             = l.get("vp_bd",{}).get("tier","C")
    tier_bg, tier_fg = TIER_COLORS.get(tier, TIER_COLORS["C"])
    preco_fmt        = lambda p: f"R$ {p:,}".replace(",",".")
    grupo_raw        = l.get("grupo","—")
    mat_label        = "carbono" if "carbono" in l.get("material","") else "alumínio"
    size_label       = l.get("size","")
    year_label       = str(l.get("year","")) if l.get("year") else ""
    city_label       = l.get("city","")
    sub_parts        = [p for p in [mat_label, f"Tam {size_label}" if size_label else "",
                                    grupo_raw, year_label, city_label] if p and p != "—"]
    subtitle         = " · ".join(sub_parts[:5])

    def cenario(label, preco, sc, vp, passa):
        cor  = "#0b7a6e" if passa else "#9b9a94"
        icone = "✓" if passa else "✗"
        return f"""
        <div style="flex:1;padding:8px 10px;background:{'#f0faf7' if passa else '#f7f6f2'};border-radius:5px;border:1px solid {'#0b7a6e44' if passa else '#e0e0e0'}">
          <div style="font-size:9px;font-family:monospace;color:#9b9a94;margin-bottom:3px">{label}</div>
          <div style="font-size:15px;font-weight:700;color:{cor}">{preco_fmt(preco)}</div>
          <div style="font-size:10px;color:{cor};margin-top:2px">{icone} score {sc} · VP {vp}</div>
        </div>"""

    c10 = cenario("−10% negociado", cond["preco_10"], cond["sc_10"], cond["vp_10"], cond["passa_10"])
    c20 = cenario("−20% negociado", cond["preco_20"], cond["sc_20"], cond["vp_20"], cond["passa_20"])

    return f"""
<div style="margin:0 16px 10px;border:1.5px solid #e8530a44;border-radius:7px;overflow:hidden;background:#fff9f6">
  <div style="padding:9px 13px 7px;border-bottom:1px solid #f0eeea">
    <div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:5px;align-items:center">
      <span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;font-size:9px;font-weight:700;font-family:monospace;background:#fff0eb;color:#7a2e00">{rank}</span>
      <span style="display:inline-block;font-size:9px;padding:2px 7px;border-radius:3px;font-family:monospace;font-weight:500;background:#fff0eb;color:#7a2e00">condicional</span>
      {chip(l["source"],src_bg,src_fg)} {chip("Tier "+tier,tier_bg,tier_fg)} {chip(cat,cat_bg,cat_fg)}
    </div>
    <div style="font-size:13px;font-weight:700;color:#1c1b18;line-height:1.3;margin-bottom:2px">{l["title"]}</div>
    <div style="font-size:11px;color:#6a6960">{subtitle}</div>
  </div>
  <div style="padding:8px 13px;border-bottom:1px solid #f0eeea">
    <div style="font-size:9px;color:#9b9a94;font-family:monospace;margin-bottom:6px">
      Preço pedido <strong style="color:#1c1b18">{preco_fmt(cond["preco_orig"])}</strong>
      · score real {cond["sc_real"]} · VP real {cond["vp_real"]}
      · abaixo do threshold — passaria com desconto:
    </div>
    <div style="display:flex;gap:8px">{c10}{c20}</div>
  </div>
  <div style="padding:8px 13px;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:10px;color:#854f0b;line-height:1.5">
      💬 Sugerir {preco_fmt(cond["preco_10"])} (−10%) {'✓ passa' if cond["passa_10"] else '✗ não passa'}<br>
      💬 Sugerir {preco_fmt(cond["preco_20"])} (−20%) {'✓ passa' if cond["passa_20"] else '✗ não passa'}
    </div>
    <a href="{l["url"]}" style="display:inline-block;background:#e8530a;color:white;font-size:11px;font-weight:700;padding:7px 13px;border-radius:5px;text-decoration:none">Ver e negociar →</a>
  </div>
</div>"""




def render_top3_cards(items: list, cat: str) -> str:
    """
    Renderiza linha de até 3 cards lado a lado para uma categoria.
    card #1 tem borda de destaque verde.
    Clique no card abre o anúncio diretamente.
    """
    if not items:
        return ""

    bg,  fg  = CAT_COLORS.get(cat, CAT_COLORS["speed"])
    label    = "speed / road" if cat == "speed" else "MTB trail 29"
    top3     = sorted(items, key=lambda x: x.get("score", 0), reverse=True)[:3]
    n_label  = f"{len(items)} oportunidade{'s' if len(items)>1 else ''} &middot; por score"

    def score_pill(val, max_val):
        if val >= 65:
            bg_p, fg_p = "#e1f5ee", "#085041"
        elif val >= 55:
            bg_p, fg_p = "#faeeda", "#633806"
        else:
            bg_p, fg_p = "#f1efe8", "#5f5e5a"
        return f'<span style="font-size:10px;font-family:monospace;font-weight:500;padding:2px 7px;border-radius:3px;background:{bg_p};color:{fg_p}">{val}</span>'

    def make_card(l, rank):
        is_top   = rank == 1
        border   = "2px solid #0b7a6e" if is_top else "0.5px solid #e0e0e0"
        rank_bg  = "#e1f5ee" if is_top else "#f4f3ef"
        rank_fg  = "#085041" if is_top else "#9b9a94"
        sc       = l.get("score", 0)
        vp       = l.get("vp",    0)
        price_int = l.get("price_int", 0) or 0
        median   = l.get("bench_median", 0) or 0
        novo_loja= l.get("vp_bd", {}).get("novo_loja", 0) or 0
        pct_used = round((median - price_int) / median * 100) if median and price_int else "?"
        pct_loja = l.get("vp_bd", {}).get("pct_desconto_loja", "?")

        src_bg, src_fg   = SOURCE_COLORS.get(l["source"], ("#f0eeea","#444"))
        tier             = l.get("vp_bd", {}).get("tier", "C")
        tier_bg, tier_fg = TIER_COLORS.get(tier, TIER_COLORS["C"])
        cat_bg2, cat_fg2 = CAT_COLORS.get(l.get("category","speed"), CAT_COLORS["speed"])
        mat      = l.get("material","")
        mat_bg   = "#f1eefe" if "carbono" in mat else "#f1f0ea"
        mat_fg   = "#534ab7" if "carbono" in mat else "#5f5e5a"
        mat_lbl  = "carbono" if "carbono" in mat else "alumínio"

        grupo_src  = l.get("grupo_source","")
        grupo_raw  = l.get("grupo","—")
        grupo_disp = f"{grupo_raw} ({'DB' if grupo_src!='titulo' else 'título'})" if grupo_src and grupo_src!='nenhum' else grupo_raw

        sub_parts = [p for p in [
            mat_lbl,
            f"Tam {l.get('size','')}" if l.get("size") else "",
            grupo_disp,
            l.get("suspensao","") if l.get("category")=="mtb" else "",
            str(l.get("year","")) if l.get("year") else "",
            l.get("city",""),
            "NF" if l.get("nf") else "",
        ] if p and p not in ("—","")]
        subtitle = " &middot; ".join(sub_parts[:5])

        return f"""
<a href="{l['url']}" style="display:flex;flex-direction:column;text-decoration:none;background:#ffffff;border:{border};border-radius:8px;overflow:hidden;flex:1;min-width:0">
  <div style="padding:10px 12px 8px;flex:1">
    <div style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;font-size:9px;font-weight:700;font-family:monospace;background:{rank_bg};color:{rank_fg};margin-bottom:6px">{rank}</div>
    <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">
      <span style="font-size:9px;padding:1px 5px;border-radius:3px;font-family:monospace;font-weight:500;background:{src_bg};color:{src_fg}">{l['source']}</span>
      <span style="font-size:9px;padding:1px 5px;border-radius:3px;font-family:monospace;font-weight:500;background:{tier_bg};color:{tier_fg}">Tier {tier}</span>
      <span style="font-size:9px;padding:1px 5px;border-radius:3px;font-family:monospace;font-weight:500;background:{mat_bg};color:{mat_fg}">{mat_lbl}</span>
    </div>
    <div style="font-size:12px;font-weight:600;color:#1c1b18;line-height:1.35;margin-bottom:3px">{l['title']}</div>
    <div style="font-size:10px;color:#6a6960;line-height:1.4">{subtitle}</div>
    <div style="display:flex;gap:5px;margin-top:7px">
      {score_pill(sc, 100)}&nbsp;<span style="font-size:9px;color:#9b9a94;font-family:monospace;padding-top:3px">score</span>
      &nbsp;{score_pill(vp, 100)}&nbsp;<span style="font-size:9px;color:#9b9a94;font-family:monospace;padding-top:3px">VP</span>
    </div>
  </div>
  <div style="padding:8px 12px;border-top:1px solid #f0eeea;display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:14px;font-weight:700;color:#0b7a6e">{l['price']}</div>
      <div style="font-size:9px;color:#9b9a94;font-family:monospace">&minus;{pct_used}% usados &middot; &minus;{pct_loja}% loja</div>
    </div>
    <span style="font-size:11px;color:#9b9a94">&#8594;</span>
  </div>
</a>"""

    # Monta os 3 slots (placeholder se < 3)
    slots = ""
    for i, l in enumerate(top3, 1):
        slots += make_card(l, i)
    # Placeholder para slots vazios
    for _ in range(3 - len(top3)):
        slots += '<div style="flex:1;min-width:0;background:#f7f6f2;border-radius:8px;border:0.5px dashed #e0e0e0;display:flex;align-items:center;justify-content:center;min-height:130px"><span style="font-size:11px;color:#b0afa8">—</span></div>'

    return f"""
  <div style="display:flex;align-items:center;gap:8px;padding:12px 16px 8px;border-bottom:1px solid #f0eeea">
    <span style="font-size:10px;font-weight:600;padding:3px 10px;border-radius:20px;background:{bg};color:{fg}">{label}</span>
    <span style="font-size:11px;color:#9b9a94;font-family:monospace">{n_label}</span>
  </div>
  <div style="display:flex;gap:8px;padding:10px 14px 14px">
    {slots}
  </div>"""

def render_condicionais_html(items: list) -> str:
    if not items:
        return ""
    blocks = ""
    for i, (l, cond) in enumerate(sorted(items, key=lambda x: max(x[1]["sc_10"],x[1]["sc_20"]), reverse=True), 1):
        blocks += render_condicional_html(l, i, cond)
    return f"""
    <div style="display:flex;align-items:center;gap:10px;padding:12px 16px 8px;border-bottom:1px solid #f0eeea;border-top:1px solid #f0eeea;margin-top:4px">
      <span style="font-size:10px;font-weight:600;padding:3px 10px;border-radius:20px;background:#fff0eb;color:#7a2e00">💬 oportunidades condicionais</span>
      <span style="font-size:11px;color:#9b9a94;font-family:monospace">{len(items)} anúncio{'s' if len(items)>1 else ''} · passam com negociação</span>
    </div>
    {blocks}"""

def build_email_html(listings: list, run_time: str, total_analyzed: int, benchmarks: dict, condicionais: list | None = None) -> str:
    n      = len(listings)
    speeds = [l for l in listings if l.get("category") == "speed"]
    mtbs   = [l for l in listings if l.get("category") == "mtb"]
    bench_date = benchmarks.get("updated_at", "?")
    bench_conf = benchmarks.get("confidence", "?")

    def render_category(items: list, cat: str) -> str:
        if not items:
            return ""
        return render_top3_cards(items, cat)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Bike Radar — {n} oportunidade{'s' if n!=1 else ''}</title></head>
<body style="margin:0;padding:16px;background:#f4f3ef;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif">
<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;border:0.5px solid #e0e0e0">

  <div style="background:#1c1b18;padding:20px 24px 18px">
    <div style="font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:rgba(255,255,255,.3);margin-bottom:6px;font-family:monospace">bike radar · vgfalcao@gmail.com</div>
    <div style="font-size:20px;font-weight:700;color:#f4f3ef;letter-spacing:-.02em">{n} oportunidade{'s' if n!=1 else ''} no radar</div>
    <div style="font-size:10px;color:rgba(255,255,255,.35);margin-top:6px;font-family:monospace">{run_time} · {len(speeds)} speed · {len(mtbs)} MTB · {total_analyzed} anúncios analisados</div>
  </div>

  <div style="display:flex;border-bottom:1px solid #f0eeea">
    <div style="flex:1;padding:10px 14px;border-right:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Analisados</div>
      <div style="font-size:17px;font-weight:700;color:#1c1b18">{total_analyzed}</div>
    </div>
    <div style="flex:1;padding:10px 14px;border-right:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Notificados</div>
      <div style="font-size:17px;font-weight:700;color:#0b7a6e">{n}</div>
    </div>
    <div style="flex:1;padding:10px 14px;border-right:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Speed</div>
      <div style="font-size:17px;font-weight:700;color:#3c3489">{len(speeds)}</div>
    </div>
    <div style="flex:1;padding:10px 14px;border-right:1px solid #f0eeea">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">MTB 29</div>
      <div style="font-size:17px;font-weight:700;color:#27500a">{len(mtbs)}</div>
    </div>
    <div style="flex:1;padding:10px 14px">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#9b9a94;font-family:monospace;margin-bottom:2px">Benchmarks</div>
      <div style="font-size:13px;font-weight:700;color:#0b7a6e">✓</div>
      <div style="font-size:9px;color:#9b9a94;font-family:monospace">{bench_date}</div>
    </div>
  </div>

  {render_category(speeds, 'speed')}
  {render_category(mtbs,   'mtb')}
  {render_condicionais_html(condicionais or [])}

  <div style="background:#f7f6f2;padding:12px 24px;border-top:1px solid #edecea;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:9px;color:#9b9a94;font-family:monospace;line-height:1.7">
      bike-radar · vgfalcao@gmail.com · github actions<br>
      benchmarks usados: {bench_date} ({bench_conf}) · novos: trimestral<br>
      fontes: olx · bazarbikes · semexe
    </div>
    <div style="font-size:9px;color:#9b9a94;font-family:monospace">
      <span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:#0b7a6e;vertical-align:middle;margin-right:4px"></span>
      2x/semana · ter + sex 10h BRT
    </div>
  </div>

</div>
</body></html>"""


# ──────────────────────────────────────────────────────────────
# ENVIO DE E-MAIL
# ──────────────────────────────────────────────────────────────

def send_email(listings: list, benchmarks: dict, total_analyzed: int, condicionais: list | None = None):
    if not CONFIG["email_pass"]:
        log.warning("EMAIL_PASS não configurado — imprimindo no stdout")
        for l in listings:
            print(f"\n[{l['source']}] {l['title']}")
            print(f"  Score: {l.get('score')}  VP: {l.get('vp')}  Preço: {l['price']}")
            print(f"  URL: {l['url']}")
        return

    now     = datetime.now().strftime("%d/%m/%Y %H:%M")
    n       = len(listings)
    nc = len(condicionais) if condicionais else 0
    subject = f"🚴 {n} oportunidade{'s' if n!=1 else ''}{f' + {nc} condicional{chr(105)+chr(115) if nc!=1 else chr(108)}' if nc else ''} — Bike Radar {now}"
    html    = build_email_html(listings, now, total_analyzed, benchmarks, condicionais)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = CONFIG["email_to"]
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(CONFIG["email_from"], CONFIG["email_pass"])
            smtp.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
        log.info(f"E-mail enviado: {subject}")
    except Exception as e:
        log.error(f"Falha no envio: {e}")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    diag = os.environ.get("DIAG", "0") == "1"  # ativa via secret DIAG=1
    log.info("Bike Monitor iniciando...")
    seen       = load_seen()
    benchmarks = load_benchmarks()
    bike_db    = load_db("bikes_database.json")
    log.info(f"IDs já vistos: {len(seen)}")

    # Coleta bruta
    raw = []
    raw.extend(scrape_bazarbikes(BAZARBIKES_ENDPOINTS))
    raw.extend(scrape_bikemagazine())
    raw.extend(scrape_semexe(SEMEXE_URLS))
    raw.extend(scrape_olx(BUSCA_OLX))
    log.info(f"Total bruto coletado: {len(raw)}")

    if diag:
        log.info("── DIAGNÓSTICO: primeiros 10 anúncios coletados ──")
        for l in raw[:10]:
            log.info(f"  [{l['source']}] {l['title'][:70]} | {l['price']}")

    # Filtra novos
    novos = [l for l in raw if l["id"] not in seen]
    log.info(f"Novos (não vistos antes): {len(novos)}")

    # Deduplica por ID dentro do batch
    deduped, ids_batch = [], set()
    for l in novos:
        if l["id"] not in ids_batch:
            deduped.append(l)
            ids_batch.add(l["id"])

    log.info(f"Após deduplicação: {len(deduped)} anúncios para avaliar")

    # Enriquece + score — com diagnóstico detalhado
    oportunidades  = []
    filtrados      = []   # eliminados por filtro
    abaixo_thresh  = []   # passaram filtro mas score/vp baixo

    for l in deduped:
        enriched = enrich(l, benchmarks, bike_db)

        if enriched is None:
            filtrados.append(l)
            continue

        sc = enriched.get("score", 0)
        vp = enriched.get("vp", 0)

        if sc >= CONFIG["score_min"] and vp >= CONFIG["vp_min"]:
            oportunidades.append(enriched)
        else:
            abaixo_thresh.append(enriched)

    # ── Resumo sempre visível nos logs ──────────────────────────
    log.info(f"")
    log.info(f"══ RESULTADO ═══════════════════════════════════════")
    log.info(f"  Coletados:         {len(raw)}")
    log.info(f"  Novos (não vistos):{len(novos)}")
    log.info(f"  Deduplicados:      {len(deduped)}")
    log.info(f"  Eliminados filtro: {len(filtrados)}")
    log.info(f"  Abaixo threshold:  {len(abaixo_thresh)}")
    log.info(f"  OPORTUNIDADES:     {len(oportunidades)}")
    log.info(f"═════════════════════════════════════════════════════")
    log.info("── Verificando oportunidades condicionais ──")

    # ── Diagnóstico: mostra POR QUÊ cada anúncio foi eliminado ──
    if diag or (len(oportunidades) == 0 and len(deduped) > 0):
        log.info("")
        log.info("── DIAGNÓSTICO: anúncios eliminados por filtro (primeiros 15) ──")
        for l in filtrados[:15]:
            log.info(f"  FILTRADO | [{l['source']}] {l['title'][:65]}")

        log.info("")
        log.info("── DIAGNÓSTICO: anúncios abaixo do threshold (primeiros 20) ──")
        for e in sorted(abaixo_thresh, key=lambda x: x.get("score",0), reverse=True)[:20]:
            sc = e.get("score",0)
            vp = e.get("vp",0)
            gr = e.get("grupo","—")
            gr_src = e.get("grupo_source","")
            db_match = "DB✓" if e.get("db_match") else "DB✗"
            alerta = " ⚠GRUPO?" if e.get("grupo_nao_confirmado") else ""
            log.info(f"  score={sc:3d} vp={vp:3d} {db_match}{alerta} | grupo={gr[:20]} ({gr_src}) | [{e['source']}] {e['title'][:55]}")

        if oportunidades:
            log.info("")
            log.info("── DIAGNÓSTICO: oportunidades encontradas ──")
            for e in oportunidades:
                log.info(f"  score={e.get('score')} vp={e.get('vp')} | {e['title'][:60]} | {e['price']}")

    # ── Oportunidades condicionais (não passam real, passam com desconto) ──
    condicionais = []
    for e in abaixo_thresh:
        cond = is_oportunidade_condicional(e, benchmarks)
        if cond:
            condicionais.append((e, cond))
            sc10, vp10 = cond["sc_10"], cond["vp_10"]
            sc20, vp20 = cond["sc_20"], cond["vp_20"]
            log.info(f"  CONDICIONAL | −10%: score={sc10} vp={vp10} {'✓' if cond['passa_10'] else '✗'} | −20%: score={sc20} vp={vp20} {'✓' if cond['passa_20'] else '✗'} | {e['title'][:50]}")

    log.info(f"  CONDICIONAIS:      {len(condicionais)}")

    if oportunidades or condicionais:
        send_email(oportunidades, benchmarks, len(deduped), condicionais)

    # Marca como vistos:
    # - Notificados (oportunidades + condicionais) → não renotifica
    # - Filtrados hard (grupo ruim, categoria errada, preço absurdo) → não reavalia
    # - Abaixo do threshold → NÃO marca → reavalia se o vendedor baixar o preço
    ids_notificados  = {e["id"] for e in oportunidades}
    ids_condicionais = {e["id"] for (e, _) in condicionais}
    ids_filtrados    = {l["id"] for l in filtrados}
    seen.update(ids_notificados | ids_condicionais | ids_filtrados)
    save_seen(seen)
    log.info(f"Marcados como vistos: {len(ids_notificados)} notif + {len(ids_condicionais)} cond + {len(ids_filtrados)} filtrados")
    log.info(f"Reavaliados na próxima run: {len(abaixo_thresh)} abaixo do threshold")
    log.info("Concluído.")


if __name__ == "__main__":
    main()
