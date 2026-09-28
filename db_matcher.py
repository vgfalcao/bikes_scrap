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

CODIGO_MAX_LEN = 4

def _codigos(s: str) -> set:
    """
    Tokens que identificam a versao do modelo, e que o fuzzy nao pode alterar.

    Sao os curtos (ate CODIGO_MAX_LEN) e os que contem digito: "sl", "slr",
    "sl6", "caad13", "9.7". Tokens longos sao nome de modelo ("stumpjumper",
    "synapse") e continuam sujeitos ao fuzzy, que existe para tolerar typo.

    A distincao nao pode ser so digito: "emonda sl" x "emonda slr" da 0.947 de
    similaridade, nao tem digito nenhum, e sao faixas de preco diferentes.
    """
    return {t for t in s.split() if len(t) <= CODIGO_MAX_LEN or any(c.isdigit() for c in t)}


def _alias_no_texto(alias: str, texto: str) -> bool:
    """Alias presente como palavra inteira, nao como pedaco de outra."""
    return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", texto) is not None


def find_match(title: str, desc: str, db: dict,
               category: str | None = None) -> tuple[dict | None, str | None]:
    """
    Encontra no database o modelo correspondente ao anuncio.
    Retorna (modelo_dict, model_key) ou (None, None).

    1. Match direto de alias, como palavra inteira
    2. Match fuzzy, proibido de atravessar digitos

    `category` ('speed' | 'mtb') restringe o pool. Sem isso o pool juntava as
    duas categorias e nada filtrava depois: "Bicicleta MTB Trek Emonda" casava
    com trek_emonda_s, um modelo de estrada, e o anuncio herdava specs de road.
    """
    text = norm(title + " " + desc)

    if category in ("speed", "mtb"):
        all_models = dict(db.get(category, {}))
    else:
        # Sem categoria conhecida, ainda assim evita que uma chave repetida em
        # mtb sobrescreva silenciosamente a de speed.
        all_models = {}
        for cat in ("speed", "mtb"):
            for k, v in db.get(cat, {}).items():
                all_models.setdefault(f"{cat}:{k}", v)

    # Etapa 1: alias como palavra inteira, preferindo o mais longo (especifico)
    best_match = best_key = None
    best_len = 0
    for model_key, model in all_models.items():
        for alias in model.get("aliases", []):
            a = norm(alias)
            if len(a) > best_len and _alias_no_texto(a, text):
                best_match, best_key, best_len = model, model_key, len(a)

    if best_match:
        return best_match, best_key.split(":")[-1] if ":" in str(best_key) else best_key

    # Etapa 2: fuzzy, para typo de grafia — nunca para versao de modelo.
    #
    # O cutoff 0.82 nao distingue versao: "cannondale caad4" x "cannondal
    # caad13" da 0.875, "caad10" x "caad13" da 0.833, "tarmac sl6" x "tarmac
    # sl7" da 0.900, "emonda sl" x "emonda slr" da 0.947. Foi assim que um
    # CAAD4 do ano 2000 casou com o CAAD13 e recebeu grupo 105 R7100. Exigir
    # os tokens de codigo identicos preserva o proposito original
    # ("stumjumper" -> "stumpjumper") sem trocar um modelo por outro.
    all_aliases = [(norm(alias), model_key, model)
                   for model_key, model in all_models.items()
                   for alias in model.get("aliases", [])]

    words = text.split()
    for window_size in [4, 3, 2]:
        for i in range(len(words) - window_size + 1):
            window = " ".join(words[i:i+window_size])
            if len(window) < 6:
                continue
            cod_win = _codigos(window)
            candidatos = [a for a, _, _ in all_aliases if _codigos(a) == cod_win]
            if not candidatos:
                continue
            matches = difflib.get_close_matches(window, candidatos, n=1, cutoff=0.82)
            if matches:
                for alias_norm, model_key, model in all_aliases:
                    if alias_norm == matches[0]:
                        k = str(model_key)
                        return model, k.split(":")[-1] if ":" in k else k

    return None, None


MAX_DIST_ANO = 3

def get_year_data(model: dict, year: int | None) -> tuple[dict, int]:
    """
    Retorna (ano_data, ano_usado), ou ({}, 0) quando nao ha ano proximo o
    bastante para confiar nas specs.

    Antes a funcao caia sempre no ultimo ano mapeado, sem limite de distancia:
    um anuncio de 2000 recebia as specs de 2024. Combinado com um match errado,
    foi assim que um CAAD4 do ano 2000 ganhou grupo 105 R7100, lancado em 2022.
    Agora a distancia maxima e MAX_DIST_ANO, e fora dela o anuncio fica sem
    specs de fabrica em vez de receber as do modelo atual.
    """
    anos = model.get("anos", {})
    if not anos:
        return {}, 0

    anos_int = {int(k): v for k, v in anos.items()}
    sorted_years = sorted(anos_int.keys())

    if year and year in anos_int:
        return anos_int[year], year

    if year:
        # Ano nao mapeado: o mapeado mais proximo, em qualquer direcao, desde
        # que dentro da janela. Um anuncio de 2025 de um modelo mapeado ate
        # 2024 e legitimo; um de 2000 nao e.
        mais_proximo = min(sorted_years, key=lambda y: abs(y - year))
        if abs(mais_proximo - year) <= MAX_DIST_ANO:
            return anos_int[mais_proximo], mais_proximo
        return {}, 0

    # Sem ano no anuncio: usa o ultimo mapeado, mas isso e um palpite — quem
    # chama marca o resultado como nao confirmado.
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
    model, model_key = find_match(title, desc, db, attrs.get("category"))

    def _sem_specs(attrs: dict) -> dict:
        """Sem match utilizavel: o titulo e a unica fonte de grupo."""
        attrs["db_match"]             = False
        attrs["grupo_source"]         = "titulo" if attrs.get("grupo") else "nenhum"
        attrs["grupo_nao_confirmado"] = not attrs.get("grupo")
        return attrs

    if not model:
        return _sem_specs(attrs)

    # Match encontrado
    year     = attrs.get("year")
    ano_data, ano_usado = get_year_data(model, year)

    # Ano do anuncio longe demais de qualquer ano mapeado: melhor nao ter
    # specs do que herdar as de outra geracao do modelo.
    if not ano_data:
        return _sem_specs(attrs)

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
