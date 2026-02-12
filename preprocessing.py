# processor.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd
from unidecode import unidecode


DEFAULT_STOPWORDS = {
    "de", "da", "do", "das", "dos", "para", "por", "em", "no", "na",
    "e", "a", "o", "as", "os", "um", "uma", "ao",

    "compra", "cartao", "credito", "debito", "online", "pagamento", "parcela",
    "pix", "transferencia", "ted", "doc", "br", "ltda", "mei", "me",
    "servico", "servicos", "assinatura", "mensalidade",
    "estabelecimento", "loj", "loja",
}

TOKEN_MIN_LEN = 2
MERCHANT_KEY_MAX_TOKENS = 4


def normalize_text(text: str) -> str:
    if text is None:
        return ""

    s = str(text).strip().lower()
    s = unidecode(s)
    s = re.sub(r"[*_\-\/\\|:;.,(){}\[\]<>]+", " ", s)
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^a-z\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokenize(text: str, stopwords: Optional[set] = None) -> List[str]:
    if stopwords is None:
        stopwords = DEFAULT_STOPWORDS

    s = normalize_text(text)
    if not s:
        return []

    tokens = []
    for t in s.split():
        if len(t) < TOKEN_MIN_LEN:
            continue
        if t in stopwords:
            continue
        tokens.append(t)
    return tokens


def build_merchant_key(description: str, max_tokens: int = MERCHANT_KEY_MAX_TOKENS) -> str:
    toks = tokenize(description)
    if not toks:
        return ""
    return " ".join(toks[:max_tokens])


@dataclass
class Rule:
    pattern_raw: str
    pattern: re.Pattern
    categoria: str
    subcategoria: str
    prioridade: int
    ativo: bool


def _compile_rule(pattern_raw: str) -> re.Pattern:
    p = (pattern_raw or "").strip()
    if not p:
        return re.compile(r"a^")

    if p.lower().startswith("re:"):
        regex = p[3:].strip()
        return re.compile(regex, flags=re.IGNORECASE)

    lit = re.escape(p.lower())
    return re.compile(rf"(^|\s){lit}(\s|$)", flags=re.IGNORECASE)


def compile_config(df_config: pd.DataFrame) -> List[Rule]:
    if df_config is None or df_config.empty:
        return []

    df = df_config.copy()
    for col in ["pattern", "categoria", "subcategoria"]:
        if col not in df.columns:
            raise ValueError(f"Config precisa ter coluna '{col}'")

    if "prioridade" not in df.columns:
        df["prioridade"] = 100
    if "ativo" not in df.columns:
        df["ativo"] = True

    rules: List[Rule] = []
    for _, row in df.iterrows():
        ativo = str(row.get("ativo", True)).strip().lower() not in {"false", "0", "nao", "não"}
        prio = pd.to_numeric(row.get("prioridade", 100), errors="coerce")
        # prio = 100 if pd.isna(prio_raw) else int(prio_raw)
        if pd.isna(prio):
            prio = 100

        pat_raw = str(row.get("pattern", "")).strip()
        categoria = str(row.get("categoria", "")).strip()
        subcategoria = str(row.get("subcategoria", "")).strip()

        rules.append(
            Rule(
                pattern_raw=pat_raw,
                pattern=_compile_rule(pat_raw),
                categoria=categoria,
                subcategoria=subcategoria,
                prioridade=int(prio),
                ativo=ativo,
            )
        )

    rules.sort(key=lambda r: (r.prioridade, -len(r.pattern_raw)))
    return rules


def classify_merchant_key(merchant_key: str, rules: List[Rule]) -> Tuple[str, str, str]:
    mk = normalize_text(merchant_key)
    if not mk:
        return ("Não classificado", "", "nao_classificado")

    for rule in rules:
        if not rule.ativo:
            continue
        if rule.pattern.search(mk):
            return (rule.categoria, rule.subcategoria, "regex_config")

    return ("Não classificado", "", "nao_classificado")


def process_and_classify(
    df_raw: pd.DataFrame,
    df_config: pd.DataFrame,
    description_col: str = "descricao",
) -> pd.DataFrame:
    df = df_raw.copy()

    if description_col not in df.columns:
        raise ValueError(f"CSV precisa ter a coluna '{description_col}' (ajuste no app.py)")

    rules = compile_config(df_config)

    df["descricao_normalizada"] = df[description_col].astype(str).map(normalize_text)
    df["merchant_key"] = df[description_col].astype(str).map(build_merchant_key)

    cats = []
    subcats = []
    methods = []

    for mk in df["merchant_key"].astype(str).tolist():
        c, s, m = classify_merchant_key(mk, rules)
        cats.append(c)
        subcats.append(s)
        methods.append(m)

    df["Categoria"] = cats
    df["Subcategoria"] = subcats
    df["MetodoClassificacao"] = methods

    return df

