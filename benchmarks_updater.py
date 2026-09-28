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
# Minimo de anuncios distintos num bucket para confiar na sua distribuicao.
MIN_SAMPLES = 8
# Precos fora disto sao erro de digitacao ou placeholder, nao mercado.
PRICE_FLOOR = 800
PRICE_CEIL = 80000

# Chaves que a calibracao pode reescrever. novo_loja/novo_ml ficam de fora.
CALIBRAVEIS = ("p25", "median", "p75")


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


def _percentis(precos: list) -> dict | None:
    """p25/median/p75 de uma amostra. Percentis ja sao robustos a outlier."""
    if len(precos) < MIN_SAMPLES:
        return None
    ordenados = sorted(precos)
    q = statistics.quantiles(ordenados, n=4, method="inclusive")
    return {
        "p25": int(round(q[0])),
        "median": int(round(statistics.median(ordenados))),
        "p75": int(round(q[2])),
    }


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

    relatorio = {"calibrados": [], "amostra_insuficiente": [], "desconhecidos": []}
    total_buckets = 0

    for cat in ("speed", "mtb"):
        for bench_key, ref in novo.get(cat, {}).items():
            if not isinstance(ref, dict):
                continue
            total_buckets += 1
            precos = [s["p"] for s in samples.get(f"{cat}:{bench_key}", {}).values()]
            calc = _percentis(precos)
            if calc is None:
                relatorio["amostra_insuficiente"].append((f"{cat}:{bench_key}", len(precos)))
                continue
            antes = {k: ref.get(k) for k in CALIBRAVEIS}
            ref.update(calc)
            ref["n"] = len(precos)
            ref["calibrado_em"] = hoje
            relatorio["calibrados"].append({
                "bucket": f"{cat}:{bench_key}", "n": len(precos),
                "antes": antes, "depois": calc,
            })

    # Buckets observados que nao existem em benchmarks.json — nao inventamos
    # novo_loja para eles, mas registramos para o log.
    conhecidos = {f"{c}:{k}" for c in ("speed", "mtb") for k in novo.get(c, {})}
    for bucket, alvo in samples.items():
        if bucket not in conhecidos and len(alvo) >= MIN_SAMPLES:
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
            delta = f" ({pct:+.0f}% vs anterior)"
        log.info(f"  ✓ {c['bucket']:<28} n={c['n']:<4} "
                 f"median {a.get('median')} → {d['median']}{delta}")
    for bucket, n in sorted(ins, key=lambda x: -x[1]):
        log.info(f"  · {bucket:<28} n={n} (precisa de {MIN_SAMPLES}) — mantém valor anterior")
    for bucket, n in rel["desconhecidos"]:
        log.info(f"  ! {bucket:<28} n={n} — observado mas ausente de benchmarks.json")
