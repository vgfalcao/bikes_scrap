"""
db_matcher.py — Motor de matching título × database de bikes

Lógica de conflito (definida pelo usuário):
  - Título vence se grupo declarado for MELHOR que o OEM do database
  - Database vence se grupo do título for pior que o OEM
  - Sem ano no título → usa último ano mapeado do modelo
  - Sem match E sem grupo no título → passa com alerta 'grupo_nao_confirmado'

Uso:
    from db_matcher import enrich_from_db
    attrs = enrich_from_db(title, desc, attrs, db)
"""

import json
import re
import difflib
from pathlib import Path


# ──────────────────────────────────────────────────────────────
# RANKING DE GRUPOS (índice menor = melhor)
# ──────────────────────────────────────────────────────────────

GRUPO_RANK_SPEED = [
    "dura-ace di2", "ultegra di2", "105 di2",
    "sram red etap", "sram force etap",
    "dura-ace", "ultegra r8100", "ultegra r8000", "ultegra",
    "sram force", "sram red",
    "105 r7100", "105 r7000", "sram rival etap",
    "105 5800", "105 5700", "105",
    "sram rival", "tiagra", "sora", "claris",
]

GRUPO_RANK_MTB = [
    "xtr m9100", "xtr",
    "xx1 axs", "xx1 eagle", "xx1",
    "x01 axs", "x01 eagle", "x01",
    "xt m8100", "xt m8000", "xt",
    "gx eagle", "gx",
    "slx m7100", "slx",
    "nx eagle", "nx",
    "deore m6100",
    "deore m5100", "deore m4100", "deore",
    "alivio", "acera", "altus", "tourney",
]


def grupo_rank(grupo: str, category: str) -> int:
    """Retorna o rank do grupo (menor = melhor). 999 = desconhecido."""
    g   = grupo.lower().strip()
    lst = GRUPO_RANK_SPEED if category == "speed" else GRUPO_RANK_MTB
    for i, k in enumerate(lst):
        if k in g:
            return i
    return 999


def resolve_grupo(grupo_titulo: str | None, grupo_db: str, category: str) -> tuple[str, str]:
    """
    Resolve qual grupo usar e retorna (grupo_final, fonte).
    fonte: 'titulo' | 'database' | 'database_fallback'
    """
    if not grupo_titulo:
        return grupo_db, "database"

    rank_t = grupo_rank(grupo_titulo, category)
    rank_d = grupo_rank(grupo_db,     category)

    # Título vence se for MELHOR (rank menor) ou igual
    if rank_t <= rank_d:
        return grupo_titulo, "titulo"
    else:
        # Título é pior — database vence (conservador)
        return grupo_db, "database"


# ──────────────────────────────────────────────────────────────
# NORMALIZAÇÃO DE TEXTO
# ──────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    s = s.lower()
    # Remove acentos comuns
    for a, b in [("é","e"),("ê","e"),("ã","a"),("â","a"),("ó","o"),("ô","o"),("ú","u"),("í","i"),("ç","c")]:
        s = s.replace(a, b)
    # Normaliza separadores
    s = re.sub(r"[\-_:/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ──────────────────────────────────────────────────────────────
# MATCHING TÍTULO × DATABASE
# ──────────────────────────────────────────────────────────────

def find_match(title: str, desc: str, db: dict) -> tuple[dict | None, str | None]:
    """
    Tenta encontrar um modelo no database que corresponda ao título.
    Retorna (modelo_dict, model_key) ou (None, None).

    Estratégia em 3 etapas:
    1. Match exato de alias normalizado
    2. Match parcial (alias contido no título)
    3. Match fuzzy (similaridade > 0.82) — captura typos como "stumjumper"
    """
    text = norm(title + " " + desc)
    all_models = {**db.get("speed", {}), **db.get("mtb", {})}

    # Etapa 1 & 2: busca direta de aliases
    best_match = None
    best_key   = None
    best_len   = 0  # preferir alias mais longo (mais específico)

    for model_key, model in all_models.items():
        for alias in model.get("aliases", []):
            a = norm(alias)
            if a in text and len(a) > best_len:
                best_match = model
                best_key   = model_key
                best_len   = len(a)

    if best_match:
        return best_match, best_key

    # Etapa 3: fuzzy matching nos aliases
    all_aliases = []
    for model_key, model in all_models.items():
        for alias in model.get("aliases", []):
            all_aliases.append((norm(alias), model_key, model))

    # Divide o título em janelas de 2-4 palavras para matching fuzzy
    words = text.split()
    for window_size in [4, 3, 2]:
        for i in range(len(words) - window_size + 1):
            window = " ".join(words[i:i+window_size])
            if len(window) < 6:
                continue
            matches = difflib.get_close_matches(window, [a[0] for a in all_aliases], n=1, cutoff=0.82)
            if matches:
                matched_alias = matches[0]
                for alias_norm, model_key, model in all_aliases:
                    if alias_norm == matched_alias:
                        return model, model_key

    return None, None


def get_year_data(model: dict, year: int | None) -> tuple[dict, int]:
    """
    Retorna (ano_data, ano_usado).
    Se ano=None ou não encontrado → usa último ano mapeado.
    """
    anos = model.get("anos", {})
    if not anos:
        return {}, 0

    anos_int = {int(k): v for k, v in anos.items()}
    sorted_years = sorted(anos_int.keys())

    if year and year in anos_int:
        return anos_int[year], year

    # Ano não mapeado exato: usa o mais próximo anterior
    if year:
        anteriores = [y for y in sorted_years if y <= year]
        if anteriores:
            y = max(anteriores)
            return anos_int[y], y

    # Sem ano: usa último mapeado
    last = sorted_years[-1]
    return anos_int[last], last


# ──────────────────────────────────────────────────────────────
# ENRIQUECIMENTO PRINCIPAL
# ──────────────────────────────────────────────────────────────

def enrich_from_db(title: str, desc: str, attrs: dict, db: dict) -> dict:
    """
    Tenta fazer match do anúncio com o database.
    Enriquece attrs com specs de fábrica e aplica lógica de resolução de grupo.

    Campos adicionados/modificados em attrs:
      - db_match (bool)
      - db_model_key (str)
      - db_model_name (str)
      - db_year_used (int)
      - db_grupo_oem (str)
      - grupo_source ('titulo' | 'database' | 'database_fallback')
      - grupo_nao_confirmado (bool) — alerta no e-mail
      - material (enriquecido se não detectado)
      - peso_db (float)
      - suspensao_db (str) — MTB
    """
    model, model_key = find_match(title, desc, db)

    if not model:
        # Sem match no database
        grupo_titulo = attrs.get("grupo")
        if not grupo_titulo:
            attrs["db_match"]              = False
            attrs["grupo_nao_confirmado"]  = True
            attrs["grupo_source"]          = "nenhum"
        else:
            attrs["db_match"]              = False
            attrs["grupo_nao_confirmado"]  = False
            attrs["grupo_source"]          = "titulo"
        return attrs

    # Match encontrado
    year     = attrs.get("year")
    ano_data, ano_usado = get_year_data(model, year)
    category = model.get("categoria", attrs.get("category", "speed"))

    grupo_oem    = ano_data.get("grupo_oem", "")
    grupo_titulo = attrs.get("grupo")

    grupo_final, grupo_source = resolve_grupo(grupo_titulo, grupo_oem, category)

    # Enriquece material — database só sobrescreve se título/desc
    # NÃO contiver sinal explícito de alumínio.
    # Razão: "Trek Émonda ALR" tem "alr" → alumínio correto.
    # O DB mapearia como carbono (último ano) e inflaria o score.
    mat_db    = ano_data.get("material", "")
    mat_atual = attrs.get("material", "aluminio")
    text_full = norm(attrs.get("title","") + " " + attrs.get("desc",""))
    alu_explicit = any(k in text_full for k in [
        "aluminum","aluminium","alpha aluminum","smartform",
        "ultralight 300","ultralight 500","alr","caad",
        "6061","6069","7005","aluminio"
    ])
    if mat_db and (mat_atual == "aluminio" and "carbono" in mat_db) and not alu_explicit:
        attrs["material"] = mat_db  # database corrige apenas quando não há sinal explícito de alu

    # Peso: usa database se não declarado no título
    peso_db = ano_data.get("peso_kg")
    if peso_db and not attrs.get("weight"):
        attrs["weight"] = peso_db

    # Suspensão MTB: usa database se não detectada no título
    susp_db = ano_data.get("suspensao")
    if susp_db and not attrs.get("suspensao"):
        attrs["suspensao"] = susp_db

    # Canote MTB
    canote_db = ano_data.get("canote_retratil")
    if canote_db is not None and not attrs.get("canote_detectado"):
        attrs["canote_db"] = canote_db

    # Freio
    freio_db = ano_data.get("freio")
    if freio_db:
        attrs["freio_db"] = freio_db

    # Escreve resultado do match
    attrs["db_match"]             = True
    attrs["db_model_key"]         = model_key
    attrs["db_model_name"]        = f"{model['marca'].title()} {model['modelo']}"
    attrs["db_year_used"]         = ano_usado
    attrs["db_grupo_oem"]         = grupo_oem
    attrs["grupo"]                = grupo_final
    attrs["grupo_source"]         = grupo_source
    attrs["grupo_nao_confirmado"] = False
    attrs["tier"]                 = model.get("tier", "C")  # tier da marca via database
    if not attrs.get("brand"):  # propaga marca do DB se não detectada no título
        attrs["brand"] = model.get("marca")

    return attrs


# ──────────────────────────────────────────────────────────────
# LOADER
# ──────────────────────────────────────────────────────────────

def load_db(path: str = "bikes_database.json") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"bikes_database.json não encontrado em {path}")
    return json.loads(p.read_text())


# ──────────────────────────────────────────────────────────────
# TESTES INLINE
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = load_db("bikes_database.json")

    casos = [
        # (título, desc, grupo_no_titulo_esperado, fonte_esperada)
        ("Cannondale SuperSix EVO 2019 54cm",         "", None,       "database"),
        ("Cannondale SuperSix EVO Ultegra 2018 54",   "", "ultegra r8000", "titulo"),
        ("speed cnd supersix evo 105 2019",           "", "105 r7000","titulo"),   # 105 vs Ultegra OEM → título vence
        ("Pinarello F4:13 2012 carbono 54cm",         "", None,       "database"),
        ("trek emonda sl6 disc 52cm 2022",            "", None,       "database"),
        ("Tarmac SL7 Shimano 105 Di2 2023",           "", "105 di2",  "titulo"),   # 105 di2 > 105 r7100 OEM → título vence
        ("stumjumper carbon 29 2021",                 "", None,       "database"),  # typo — fuzzy match
        ("Sense Exper Carbono XT 2022 tamanho M",     "", "xt m8100", "titulo"),
        ("bike speed carbono 54 sem grupo declarado", "", None,       "nenhum"),   # sem match, sem grupo → alerta
        ("CAAD10 105 5800 2016 rim brake SP",         "", "105 5800", "titulo"),   # título igual ao OEM → título
    ]

    print(f"{'TÍTULO':<48} {'GRUPO FINAL':<22} {'FONTE':<12} {'MATCH':<6} {'ANO DB'}")
    print("-" * 105)
    for title, desc, _, _ in casos:
        attrs = {"year": None, "grupo": None, "material": "aluminio", "weight": None}
        # Extrai ano do título
        m = re.search(r"\b(201[0-9]|202[0-9])\b", title)
        if m:
            attrs["year"] = int(m.group(1))
        # Simula detecção de grupo no título (simplificada)
        t = title.lower()
        for g in ["dura-ace di2","ultegra di2","105 di2","ultegra r8000","ultegra","105 r7100","105 r7000","105 5800","105 5700","xt m8100","xt m8000","slx m7100","gx eagle","deore m6100"]:
            if g in t:
                attrs["grupo"] = g
                break
        result = enrich_from_db(title, desc, attrs, db)
        grupo  = result.get("grupo") or "—"
        fonte  = result.get("grupo_source","—")
        match  = "✓" if result.get("db_match") else "✗"
        alerta = " ⚠ ALERTA" if result.get("grupo_nao_confirmado") else ""
        ano_db = result.get("db_year_used","—")
        print(f"{title[:47]:<48} {grupo:<22} {fonte:<12} {match:<6} {ano_db}{alerta}")
