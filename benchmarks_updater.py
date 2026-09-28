"""
Auto-calibracao dos benchmarks a partir dos proprios anuncios coletados.

Contexto: benchmarks.json foi escrito a mao em 2025-06-01 e nunca teve
mecanismo de atualizacao — o scraper.py prometia um "benchmarks_updater.py
1x/mes" que jamais existiu. Este modulo e esse mecanismo.

Como funciona: cada run guarda (bucket, preco) de TODOS os anuncios que
passaram pelos filtros duros — nao so das oportunidades, que sao um recorte
enviesado para baixo. Com amostra suficiente numa janela movel, recalcula
p25/median/p75 do bucket.

O que este modulo NAO faz: novo_loja e novo_ml sao precos de varejo e nao se
derivam de anuncio de usado. Continuam vindo do valor escrito a mao e
precisam de atualizacao manual ou de outra fonte.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("bike-monitor")

HISTORY_FILE = "bench_history.json"

# Janela movel de amostras consideradas no calculo.
WINDOW_DAYS = 180

# Minimo de anuncios para calibrar a MEDIANA de um bucket direto da sua amostra.
MIN_SAMPLES = 12
# Quartis tem muito mais variancia que a mediana: com n=13 o p25 do primeiro
# bucket calibrado caiu 46% num unico run, o que era ruido, nao mercado.
MIN_SAMPLES_QUARTIS = 25
# Minimo para estimar a deriva de um GRUPO (ver GRUPOS abaixo).
MIN_SAMPLES_GRUPO = 12
# Minimo para avisar que um bucket observado nao existe em benchmarks.json.
MIN_SAMPLES_DESCONHECIDO = 5

# Precos fora disto sao erro de digitacao ou placeholder, nao mercado.
PRICE_FLOOR = 800
PRICE_CEIL = 80000

# Trava de dispersao: razao p75/p25 acima disto significa que o bucket esta
# juntando bikes que nao tem relacao entre si, e sua mediana nao representa
# nada. Observado em producao: mtb:alu_slx_rockshox trouxe anuncios de R$1.550
# a R$24.000 (p75/p25 = 4,2) — calibrar nisso da aparencia estatistica a lixo.
# A causa esta na atribuicao de bench_key, nao aqui; esta trava so impede que
# o problema se propague para o score.
MAX_DISPERSAO = 3.0

# Chaves que a calibracao pode reescrever. novo_loja/novo_ml ficam de fora.
CALIBRAVEIS = ("p25", "median", "p75")

# Agrupamento por material, que e o fator dominante de preco. Serve para
# estimar DERIVA, nao nivel: buckets do mesmo grupo continuam com medianas
# diferentes entre si. Sem isso os buckets de cauda (alu_rival aparece 0 vezes
# num run tipico) nunca acumulariam amostra e ficariam congelados para sempre.
GRUPOS = {
    "speed": {
        "alu":     ["alu_105", "alu_ultegra", "alu_rival"],
        "carbono": ["carbono_105", "carbono_ultegra", "carbono_di2"],
    },
    "mtb": {
        "alu":     ["alu_slx_rockshox", "alu_xt_fox"],
        "carbono": ["carbono_slx", "carbono_xt_fox", "carbono_xtr_eagle"],
    },
}


def grupo_de(cat: str, bench_key: str) -> str | None:
    for nome, chaves in GRUPOS.get(cat, {}).items():
        if bench_key in chaves:
            return nome
    return None


def _hoje() -> str:
    return date.today().isoformat()


def _parse_dia(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def load_history(path: str = HISTORY_FILE) -> dict:
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data.get("samples"), dict):
                return data
        except Exception:
            log.warning("bench_history.json invalido — comecando historico do zero")
    return {"updated_at": None, "samples": {}}


def save_history(history: dict, path: str = HISTORY_FILE) -> None:
    Path(path).write_text(json.dumps(history, indent=2, ensure_ascii=False))


def record_samples(enriched: list, history: dict, hoje: str | None = None) -> dict:
    """
    Registra os anuncios da run no historico.

    Uma amostra por anuncio (chave = id): se o mesmo anuncio reaparece numa run
    posterior, ou se FORCE_NOTIFY faz a run reprocessar tudo, o registro e
    sobrescrito em vez de duplicado. Sem isso um anuncio antigo e persistente
    pesaria varias vezes na mediana.
    """
    hoje = hoje or _hoje()
    samples = history.setdefault("samples", {})
    novos = atualizados = 0

    for e in enriched:
        bench_key = e.get("bench_key")
        cat = e.get("category")
        preco = e.get("price_int")
        lid = e.get("id")
        if not (bench_key and cat and lid):
            continue
        if not isinstance(preco, (int, float)) or not (PRICE_FLOOR <= preco <= PRICE_CEIL):
            continue

        bucket = f"{cat}:{bench_key}"
        alvo = samples.setdefault(bucket, {})
        if lid in alvo:
            atualizados += 1
        else:
            novos += 1
        alvo[lid] = {"p": int(preco), "d": hoje}

    history["updated_at"] = hoje
    log.info(f"Benchmarks: {novos} amostras novas, {atualizados} atualizadas")
    return history


def prune_history(history: dict, hoje: str | None = None, window: int = WINDOW_DAYS) -> dict:
    """Descarta amostras fora da janela movel, para o benchmark acompanhar o mercado."""
    ref = _parse_dia(hoje or _hoje())
    if ref is None:
        return history
    limite = ref - timedelta(days=window)
    removidos = 0

    for bucket, alvo in list(history.get("samples", {}).items()):
        for lid, s in list(alvo.items()):
            d = _parse_dia(s.get("d", ""))
            if d is None or d < limite:
                del alvo[lid]
                removidos += 1
        if not alvo:
            del history["samples"][bucket]

    if removidos:
        log.info(f"Benchmarks: {removidos} amostras fora da janela de {window}d descartadas")
    return history


def dispersao(valores: list) -> float | None:
    """Razao p75/p25. Acima de MAX_DISPERSAO a amostra nao e homogenea."""
    if len(valores) < 4:
        return None
    q = statistics.quantiles(sorted(valores), n=4, method="inclusive")
    return (q[2] / q[0]) if q[0] else None


def _percentis(precos: list, base: dict) -> dict | None:
    """
    p25/median/p75 da amostra propria do bucket.

    A mediana calibra a partir de MIN_SAMPLES; os quartis so a partir de
    MIN_SAMPLES_QUARTIS, porque tem muito mais variancia. Abaixo disso os
    quartis anteriores sao mantidos em vez de virarem ruido.
    """
    n = len(precos)
    if n < MIN_SAMPLES:
        return None
    d = dispersao(precos)
    if d and d > MAX_DISPERSAO:
        return {"_disperso": d}
    ordenados = sorted(precos)
    out = {"median": int(round(statistics.median(ordenados)))}
    if n >= MIN_SAMPLES_QUARTIS:
        q = statistics.quantiles(ordenados, n=4, method="inclusive")
        out["p25"], out["p75"] = int(round(q[0])), int(round(q[2]))
    else:
        out["p25"], out["p75"] = base.get("p25"), base.get("p75")
    return out


def _deriva_do_grupo(cat: str, grupo: str, novo: dict, samples: dict) -> tuple[float, int] | None:
    """
    Fator de deriva de preco de um grupo de buckets.

    Cada amostra e normalizada pela mediana-base do SEU bucket antes de entrar
    na conta, e a deriva e a mediana dessas razoes. Com isso:

    - buckets do mesmo grupo mantem medianas diferentes entre si (a deriva e
      multiplicativa, nao um nivel comum);
    - a composicao da amostra nao enviesa o resultado — um grupo dominado por
      bikes baratas nao puxa o fator para baixo, porque cada amostra e medida
      contra a sua propria referencia.
    """
    razoes = []
    for bench_key in GRUPOS.get(cat, {}).get(grupo, []):
        ref = novo.get(cat, {}).get(bench_key)
        if not isinstance(ref, dict):
            continue
        base = ref.get("median_base") or ref.get("median")
        if not base:
            continue
        for s in samples.get(f"{cat}:{bench_key}", {}).values():
            razoes.append(s["p"] / base)
    if len(razoes) < MIN_SAMPLES_GRUPO:
        return None
    d = dispersao(razoes)
    if d and d > MAX_DISPERSAO:
        log.info(f"  ! grupo {cat}:{grupo} disperso demais (p75/p25={d:.1f}) — "
                 f"deriva descartada")
        return None
    return statistics.median(razoes), len(razoes)


def calcular_confianca(calibrados: int, total: int, updated_at: str | None,
                       hoje: str | None = None) -> str:
    """
    Confianca derivada da cobertura real da calibracao, nao de um literal.

    Antes o campo era a string fixa "high", que seguiu dizendo "high" por
    quinze meses sem ninguem tocar nos numeros.
    """
    if total <= 0:
        return "low"
    cobertura = calibrados / total
    d_ref, d_hoje = _parse_dia(updated_at or ""), _parse_dia(hoje or _hoje())
    idade = (d_hoje - d_ref).days if (d_ref and d_hoje) else 9999

    if cobertura >= 0.60 and idade <= 30:
        return "high"
    if cobertura >= 0.30 and idade <= 90:
        return "medium"
    if cobertura > 0:
        return "low"
    return "stale"


def recalibrate(benchmarks: dict, history: dict, hoje: str | None = None) -> tuple[dict, dict]:
    """
    Recalcula p25/median/p75 dos buckets com amostra suficiente.

    Só mexe em bucket que ja existe em benchmarks.json: criar um bucket novo
    sem novo_loja quebraria o calculo de VP. novo_loja/novo_ml sao preservados
    intactos em todos os casos.

    Devolve (benchmarks_novo, relatorio).
    """
    hoje = hoje or _hoje()
    novo = json.loads(json.dumps(benchmarks))  # copia profunda
    samples = history.get("samples", {})

    relatorio = {"calibrados": [], "por_grupo": [], "amostra_insuficiente": [],
                 "dispersos": [], "desconhecidos": []}
    total_buckets = 0

    # median_base preserva o valor de referencia original de cada bucket. Sem
    # ele a deriva seria calculada contra a mediana ja calibrada e composta a
    # cada run, afastando o benchmark do mercado em vez de aproxima-lo.
    for cat in ("speed", "mtb"):
        for ref in novo.get(cat, {}).values():
            if isinstance(ref, dict) and "median_base" not in ref and ref.get("median"):
                ref["median_base"] = ref["median"]

    derivas = {}
    for cat in ("speed", "mtb"):
        for grupo in GRUPOS.get(cat, {}):
            d = _deriva_do_grupo(cat, grupo, novo, samples)
            if d:
                derivas[(cat, grupo)] = d

    for cat in ("speed", "mtb"):
        for bench_key, ref in novo.get(cat, {}).items():
            if not isinstance(ref, dict):
                continue
            total_buckets += 1
            precos = [s["p"] for s in samples.get(f"{cat}:{bench_key}", {}).values()]
            antes = {k: ref.get(k) for k in CALIBRAVEIS}

            # 1) amostra propria suficiente -> calibra direto
            calc = _percentis(precos, ref)
            era_disperso = calc is not None and "_disperso" in calc
            if era_disperso:
                relatorio["dispersos"].append(
                    (f"{cat}:{bench_key}", len(precos), calc["_disperso"]))
                calc = None
            if calc is not None:
                ref.update(calc)
                ref["n"] = len(precos)
                ref["calibrado_em"] = hoje
                ref["origem"] = "direto"
                relatorio["calibrados"].append({
                    "bucket": f"{cat}:{bench_key}", "n": len(precos),
                    "antes": antes, "depois": calc, "origem": "direto",
                })
                continue

            # 2) sem amostra propria -> aplica a deriva do grupo sobre a base
            grupo = grupo_de(cat, bench_key)
            d = derivas.get((cat, grupo))
            if d and ref.get("median_base"):
                fator, n_grupo = d
                calc = {"median": int(round(ref["median_base"] * fator)),
                        "p25": ref.get("p25"), "p75": ref.get("p75")}
                ref.update(calc)
                ref["n"] = len(precos)
                ref["calibrado_em"] = hoje
                ref["origem"] = f"grupo:{grupo}"
                relatorio["calibrados"].append({
                    "bucket": f"{cat}:{bench_key}", "n": len(precos),
                    "antes": antes, "depois": calc,
                    "origem": f"grupo:{grupo} (n={n_grupo}, x{fator:.2f})",
                })
                continue

            # 3) nem amostra propria nem grupo -> mantem o valor anterior
            #    (quem ja foi reportado como disperso nao repete aqui)
            if not era_disperso:
                relatorio["amostra_insuficiente"].append(
                    (f"{cat}:{bench_key}", len(precos)))

    # Buckets observados que nao existem em benchmarks.json — nao inventamos
    # novo_loja para eles, mas registramos para o log.
    # Limiar proprio, e mais baixo: a funcao deste aviso e sinalizar cedo que
    # falta um bucket no benchmarks.json, nao calibrar coisa nenhuma.
    conhecidos = {f"{c}:{k}" for c in ("speed", "mtb") for k in novo.get(c, {})}
    for bucket, alvo in samples.items():
        if bucket not in conhecidos and len(alvo) >= MIN_SAMPLES_DESCONHECIDO:
            relatorio["desconhecidos"].append((bucket, len(alvo)))

    novo["updated_at"] = hoje if relatorio["calibrados"] else benchmarks.get("updated_at")
    novo["confidence"] = calcular_confianca(
        len(relatorio["calibrados"]), total_buckets, novo.get("updated_at"), hoje)
    relatorio["total_buckets"] = total_buckets
    relatorio["confidence"] = novo["confidence"]
    return novo, relatorio


def log_relatorio(rel: dict) -> None:
    cal, ins = rel["calibrados"], rel["amostra_insuficiente"]
    log.info("── BENCHMARKS: calibração ──")
    log.info(f"  buckets calibrados: {len(cal)}/{rel['total_buckets']} "
             f"· confiança: {rel['confidence']}")
    for c in cal:
        a, d = c["antes"], c["depois"]
        delta = ""
        if a.get("median"):
            pct = (d["median"] - a["median"]) / a["median"] * 100
            delta = f" ({pct:+.0f}%)"
        log.info(f"  ✓ {c['bucket']:<28} n={c['n']:<4} "
                 f"median {a.get('median')} → {d['median']}{delta}  [{c['origem']}]")
    for bucket, n, d in sorted(rel["dispersos"], key=lambda x: -x[2]):
        log.warning(f"  ⚠ {bucket:<28} n={n} p75/p25={d:.1f} — amostra heterogênea "
                    f"demais, NÃO calibrado (revisar atribuição de bench_key)")
    for bucket, n in sorted(ins, key=lambda x: -x[1]):
        log.info(f"  · {bucket:<28} n={n} (precisa de {MIN_SAMPLES} próprias "
                 f"ou {MIN_SAMPLES_GRUPO} no grupo) — mantém valor anterior")
    for bucket, n in rel["desconhecidos"]:
        log.info(f"  ! {bucket:<28} n={n} — observado mas ausente de benchmarks.json")
