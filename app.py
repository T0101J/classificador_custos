# app.py
import os
import pandas as pd
import streamlit as st
import plotly.express as px
from gspread.exceptions import APIError
import hashlib
import time

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Backend (merchant_key + regex)
from preprocessing import process_and_classify


# =========================
# CONFIG BÁSICA
# =========================

st.set_page_config(page_title="Classificador de Gastos", layout="wide")
st.title("Classificador de Gastos")

# ======= Google Sheets (ajuste) =======
GOOGLE_SHEET_ID = st.secrets.get("GOOGLE_SHEET_ID", "")
DB_SHEET_TAB = st.secrets.get("DB_SHEET_TAB", "db")
CONFIG_SHEET_TAB = st.secrets.get("CONFIG_SHEET_TAB", "config")

# =========================
# UTIL: CSV DOWNLOAD
# =========================
def gerar_download_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

def make_tx_id(row: pd.Series) -> str:
    base = f"{row.get('data','')}|{row.get('valor','')}|{row.get('descricao','')}|{row.get('conta','')}|{row.get('Categoria','')}"
    base = str(base).strip().lower()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

# =========================
# GOOGLE SHEETS
# =========================
def _get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if "gcp_service_account" in st.secrets:
        creds_info = st.secrets["gcp_service_account"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    else:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not cred_path:
            raise RuntimeError("Credenciais não configuradas (secrets ou GOOGLE_APPLICATION_CREDENTIALS).")
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)

    return gspread.authorize(creds)


def gsheet_read_df(sheet_id: str, tab_name: str) -> pd.DataFrame:
    client = _get_gspread_client()
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(tab_name)
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return pd.DataFrame()
    headers = values[0]
    rows = values[1:]
    return pd.DataFrame(rows, columns=headers)


def gsheet_append_dedup(sheet_id: str, tab_name: str, df_new: pd.DataFrame, id_col: str = "tx_id"):
    """
    - Se a aba estiver vazia -> overwrite com df_new
    - Se já existir -> lê ids existentes e só appenda linhas novas
    """
    if df_new is None or df_new.empty:
        return {"acao": "nada", "novas": 0, "duplicadas": 0}

    client = _get_gspread_client()
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(tab_name)

    current = ws.get_all_values()

    # Aba vazia -> overwrite
    if not current:
        ws.clear()
        ws.append_row(list(df_new.columns), value_input_option="USER_ENTERED")
        ws.append_rows(df_new.astype(str).values.tolist(), value_input_option="USER_ENTERED")
        return {"acao": "overwrite_vazio", "novas": len(df_new), "duplicadas": 0}

    header = current[0] if len(current) > 0 else []
    header_ok = header and any(str(h).strip() for h in header)

    if not header_ok:
        ws.clear()
        ws.append_row(list(df_new.columns), value_input_option="USER_ENTERED")
        ws.append_rows(df_new.astype(str).values.tolist(), value_input_option="USER_ENTERED")
        return {"acao": "overwrite_header_invalido", "novas": len(df_new), "duplicadas": 0}

    header = [str(h).strip() for h in header]

    if id_col not in header:
        raise ValueError(f"A aba '{tab_name}' não tem a coluna '{id_col}'. Crie essa coluna no header do db.")

    # Lê somente a coluna tx_id do Sheets (evita puxar tudo)
    # current inclui header, então ids começam em current[1:]
    id_index = header.index(id_col)
    ids_existentes = set()
    for r in current[1:]:
        if len(r) > id_index:
            v = str(r[id_index]).strip()
            if v:
                ids_existentes.add(v)

    # Filtra novas
    mask_novas = ~df_new[id_col].astype(str).isin(ids_existentes)
    df_to_append = df_new[mask_novas].copy()

    duplicadas = len(df_new) - len(df_to_append)

    if df_to_append.empty:
        return {"acao": "Salvar", "novas": 0, "duplicadas": duplicadas}

    # Reordena pelo header do Sheets
    missing = [c for c in header if c not in df_to_append.columns]
    if missing:
        raise ValueError(f"A aba '{tab_name}' exige colunas que não existem no df_result: {missing}")

    ws.append_rows(df_to_append[header].astype(str).values.tolist(), value_input_option="USER_ENTERED")
    return {"acao": "append", "novas": len(df_to_append), "duplicadas": duplicadas}



def gsheet_overwrite_df(sheet_id: str, tab_name: str, df: pd.DataFrame):
    client = _get_gspread_client()
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(tab_name)
    ws.clear()
    if df.empty:
        return
    ws.append_row(list(df.columns), value_input_option="USER_ENTERED")
    ws.append_rows(df.astype(str).values.tolist(), value_input_option="USER_ENTERED")

@st.cache_data(ttl=60)  # 60s: evita ler do Sheets toda hora
def gsheet_read_df_cached(sheet_id: str, tab_name: str) -> pd.DataFrame:
    return gsheet_read_df(sheet_id, tab_name)

def load_config_from_sheets():
    if not GOOGLE_SHEET_ID:
        st.warning("Falta configurar GOOGLE_SHEET_ID nos secrets.")
        return

    df_g = gsheet_read_df_cached(GOOGLE_SHEET_ID, CONFIG_SHEET_TAB)

    if df_g.empty:
        st.warning("Aba config está vazia no Google Sheets.")
        return

    # coerção básica
    if "prioridade" in df_g.columns:
        df_g["prioridade"] = pd.to_numeric(df_g["prioridade"], errors="coerce").fillna(100).astype(int)
    if "ativo" in df_g.columns:
        df_g["ativo"] = df_g["ativo"].astype(str).str.lower().isin(["true", "1", "sim", "yes"])

    st.session_state.df_config = df_g
    
# =========================
# PADRONIZAÇÃO DO CSV (NUBANK)
# =========================
def padronizar_csv(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Ajuste aqui se você usar outra origem além do Nubank.
    Esperado no app após padronizar:
      - data
      - descricao
      - valor
      - conta
    """
    df = df_raw.copy()

    # Nubank padrão: Data, Descrição, Valor
    df = df.rename(columns={"Data": "data", "Descrição": "descricao", "Valor": "valor"})

    # Se ainda não existir, cria
    if "conta" not in df.columns:
        df["conta"] = "Nubank"

    # Normaliza valor (se vier com vírgula decimal)
    if "valor" in df.columns:
        s = df["valor"].astype(str).str.strip()

        # tenta tratar casos comuns pt-BR: "1.234,56"
        s = s.str.replace(".", ",", regex=False).str.replace(",", ".", regex=False)
        df["valor"] = pd.to_numeric(s, errors="coerce")

    return df

def coerce_config_types(df_g: pd.DataFrame) -> pd.DataFrame:
    df = df_g.copy()

    if "prioridade" in df.columns:
        df["prioridade"] = pd.to_numeric(df["prioridade"], errors="coerce").fillna(100).astype(int)

    if "ativo" in df.columns:
        df["ativo"] = df["ativo"].astype(str).str.lower().isin(["true", "1", "sim", "yes"])

    # garante colunas
    for col in ["pattern", "categoria", "subcategoria"]:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)

    return df


def load_config_from_sheets(force: bool = False):
    if not GOOGLE_SHEET_ID:
        return

    if force:
        gsheet_read_df_cached.clear()  # força puxar de novo

    df_g = gsheet_read_df_cached(GOOGLE_SHEET_ID, CONFIG_SHEET_TAB)

    if df_g.empty:
        return

    df_g = coerce_config_types(df_g)

    # config ativa (usada na classificação)
    st.session_state.df_config = df_g
    # rascunho de edição (tela)
    st.session_state.df_config_draft = df_g.copy()

# =========================
# SIDEBAR - ESTADO
# =========================
with st.sidebar:
    st.header("Estado do App")

    if "df_config" not in st.session_state:
        # seeds mínimas pra não ficar vazio
        st.session_state.df_config = pd.DataFrame(
            [
            {"pattern": "oficina", "categoria": "Oficina", "subcategoria": "Oficina", "prioridade": 10, "ativo": True},
            {"pattern": "pecas", "categoria": "Peças", "subcategoria": "Peças", "prioridade": 10, "ativo": True},
            {"pattern": "rest", "categoria": "Restaurante", "subcategoria": "Restaurante", "prioridade": 10, "ativo": True},
            {"pattern": "s10", "categoria": "Combustivel", "subcategoria": "Combustivel", "prioridade": 10, "ativo": True},
            {"pattern": "lourival da costa santos", "categoria": "Pro Labore", "subcategoria": "Pro Labore", "prioridade": 10, "ativo": True},
            {"pattern": "bezerra oliveira", "categoria": "Peças", "subcategoria": "Peças", "prioridade": 10, "ativo": True},
            {"pattern": "pedreira sao francisco", "categoria": "Brita", "subcategoria": "Brita", "prioridade": 10, "ativo": True},
            {"pattern": "js pedras", "categoria": "Areia", "subcategoria": "Areia", "prioridade": 10, "ativo": True},
            {"pattern": "minas brita", "categoria": "Brita", "subcategoria": "Brita", "prioridade": 10, "ativo": True},
            {"pattern": "brasil mineracao", "categoria": "Brita", "subcategoria": "Brita", "prioridade": 10, "ativo": True},
            {"pattern": "maranhao mineracao", "categoria": "Brita", "subcategoria": "Brita", "prioridade": 10, "ativo": True},
            {"pattern": "oficina jcj vitoria", "categoria": "Oficina", "subcategoria": "Oficina", "prioridade": 10, "ativo": True},
            {"pattern": "parafuso", "categoria": "Peças", "subcategoria": "Peças", "prioridade": 10, "ativo": True},
            {"pattern": "material construcao", "categoria": "Material Construção", "subcategoria": "Material Construção", "prioridade": 10, "ativo": True},
            {"pattern": "ferro comercio", "categoria": "Material Construção", "subcategoria": "Material Construção", "prioridade": 10, "ativo": True},
            ],
            columns=["pattern", "categoria", "subcategoria", "prioridade", "ativo"],
        )

    if "df_result" not in st.session_state:
        st.session_state.df_result = pd.DataFrame()

    st.write("Config carregada:", len(st.session_state.df_config), "regras")
    st.write("Resultado na sessão:", "sim" if not st.session_state.df_result.empty else "não")

    use_gsheets = st.toggle("Usar Google Sheets", value=bool(GOOGLE_SHEET_ID))
    st.caption("Se ativar, lê/escreve DB e CONFIG no Google Sheets.")
    with st.sidebar:
        if use_gsheets and st.button("🔎 Testar conexão Google Sheets"):
            try:
                client = _get_gspread_client()
                st.success("✅ Credenciais OK")
                if not GOOGLE_SHEET_ID:
                    st.error("Falta configurar GOOGLE_SHEET_ID nos secrets.")
                    st.stop()
                st.success("✅ GOOGLE Panilha ID encontrado")

                sh = client.open_by_key(GOOGLE_SHEET_ID)
                st.success(f"✅ Planilha aberta: {sh.title}")

                tabs = [ws.title for ws in sh.worksheets()]
                st.success(f"✅ Abas lidas: {tabs}")

                ws = sh.worksheet(CONFIG_SHEET_TAB)
                st.success(f"✅ Aba CONFIG encontrada: {ws.title}")

            except APIError as e:
                st.error("APIError (Google):")
                st.code(str(e))
            except Exception as e:
                st.error("Erro inesperado:")
                st.code(repr(e))

        # ✅ auto-load config ao iniciar (1x por sessão)
        if use_gsheets and GOOGLE_SHEET_ID:
            if "config_loaded_once" not in st.session_state:
                st.session_state.config_loaded_once = False

            if not st.session_state.config_loaded_once:
                try:
                    load_config_from_sheets()
                    st.session_state.config_loaded_once = True
                    st.sidebar.success("✅ Configurações carregadas automaticamente")
                except Exception as e:
                    st.sidebar.error(f"Erro ao carregar config automaticamente: {e}")


# =========================
# TABS
# =========================
tab_upload, tab_visualizacao, tab_config = st.tabs(["📤 Upload (CSV)", "📊 Visualização", "⚙️ Configurações"])


# -------------------------
# TAB: UPLOAD
# -------------------------
with tab_upload:
    st.subheader("Subir CSV do mês")

    uploaded = st.file_uploader(label="Envie seu extrato bancário em CSV.", type=["csv"], )

    if uploaded is not None:
        df_raw = pd.read_csv(uploaded)
        df_raw = padronizar_csv(df_raw)

        st.markdown("### Prévia do CSV (padronizado)")
        st.dataframe(df_raw.head(30), use_container_width=True)

        if st.button("Classificar"):
            st.session_state.db_append_done = False

            df_config = st.session_state.df_config.copy()

            # 1) Classifica
            df_result = process_and_classify(
                df_raw=df_raw,
                df_config=df_config,
                description_col="descricao",
            )
            st.session_state.df_result = df_result
            st.success("✅ Classificação concluída.")

            # 2) Auto-salva no Google Sheets (db) se ativado
            if use_gsheets:
                if not GOOGLE_SHEET_ID:
                    st.warning("Google Sheets ativado, mas GOOGLE_SHEET_ID não está configurado.")
                else:
                    try:
                        df_save = df_result[['data','valor','descricao','Categoria','conta']].copy()
                        df_save["tx_id"] = df_save.apply(make_tx_id, axis=1)

                        resultado = gsheet_append_dedup(
                            GOOGLE_SHEET_ID, DB_SHEET_TAB, df_save, id_col="tx_id"
                        )

                        st.success(
                            f"✅ Enviado para o Google Sheets: "
                            f"\n Novos registros: {resultado['novas']} | "
                            f"\n Registros duplicados ignorados: {resultado['duplicadas']}"
                        )

                        st.session_state.db_append_done = True

                    except APIError as e:
                        st.error("APIError (Google):")
                        st.code(str(e))
                    except Exception as e:
                        st.error("Erro ao salvar no Google Sheets:")
                        st.code(repr(e))
        
    else:
        st.info("Envie um CSV para classificar e (opcionalmente) fazer append no Google Sheets.")


# -------------------------
# TAB: CONFIG
# -------------------------
with tab_config:
    st.subheader("Configurações (regras de classificação)")

    st.info(
        "Como preencher o campo **pattern**:\n"
        "- Sem `re:` → procura o texto exatamente como você digitou.\n"
        "- Com `re:` → modo avançado (regex), para padrões mais flexíveis.\n"
        "Ex.: `re:auto|center` significa classificar auto ou center como a categoria definida"
    )

    # garante draft
    if "df_config_draft" not in st.session_state:
        st.session_state.df_config_draft = st.session_state.df_config.copy()

    df_edit = st.data_editor(
        st.session_state.df_config_draft,
        num_rows="dynamic",
        use_container_width=True,
        key="config_editor",
    )

    c1, c2 = st.columns([1, 1])


    with c1:
        if use_gsheets and st.button("☁️ Salvar no Google Sheets"):

            st.session_state.df_config_draft = df_edit
            st.session_state.df_config = df_edit.copy()
            st.success("✅ Configurações aplicadas. A classificação já usará essa versão.")
            try:
                df_to_save = df_edit.copy()
                df_to_save = coerce_config_types(df_to_save)

                gsheet_overwrite_df(GOOGLE_SHEET_ID, CONFIG_SHEET_TAB, df_to_save)

                # atualiza estados + força recarregar do cache
                st.session_state.df_config = df_to_save
                st.session_state.df_config_draft = df_to_save.copy()
                gsheet_read_df_cached.clear()

                st.success("✅ Configurações salva no Google Sheets.")
            except Exception as e:
                st.error(f"Erro ao salvar Configurações: {e}")

    with c2:
        if st.button("↩️ Descartar alterações"):
            st.session_state.df_config_draft = st.session_state.df_config.copy()
            st.info("Alterações descartadas (voltou para a config ativa).")
# -------------------------
# TAB: VISUALIZAÇÃO
# -------------------------
with tab_visualizacao:
    st.subheader("Resultados")

    df = st.session_state.df_result.copy()

    if df.empty:
        st.warning("Ainda não há resultados. Vá na aba Upload e classifique um CSV.")
    else:
        valor_col = "valor" if "valor" in df.columns else None

        # Cards
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if valor_col:
                total = pd.to_numeric(sum([value if value <0 else 0 for value in df[valor_col] ]), errors="coerce")
                st.metric("Total gasto", f"R$ {total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
            else:
                st.metric("Total gasto", "— (ajuste coluna)")

        with c2:
            total = pd.to_numeric(sum([value if value >0 else 0 for value in df[valor_col] ]), errors="coerce")
            st.metric("Total receita", f"R$ {total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))

        with c3:
            cat_unique = df["Categoria"].nunique() if "Categoria" in df.columns else 0
            st.metric("Categorias", f"{cat_unique}")

        with c4:
            nao_class = (df["Categoria"] == "Não classificado").sum() if "Categoria" in df.columns else 0
            st.metric("Não classificado", f"{nao_class}")

        st.divider()

        # Tabela
        st.markdown("### Tabela (resultado classificado)")
        st.dataframe(df[['data','valor','descricao','merchant_key','Categoria']], use_container_width=True, height=420)

        # Download
        st.download_button(
            "⬇️ Baixar resultado (CSV)",
            data=gerar_download_csv(df),
            file_name="gastos_classificados.csv",
            mime="text/csv",
        )

        st.divider()

        # =========================
        # FILTRO DE CATEGORIAS
        # =========================
        if "Categoria" in df.columns and valor_col:
            df[valor_col] = pd.to_numeric(df[valor_col], errors="coerce").fillna(0)

            categorias = sorted(df["Categoria"].dropna().unique().tolist())

            # >>> padrão: todas EXCETO "Não classificado"
            default_categorias = [c for c in categorias if c != "Não classificado"]

            st.markdown("### Filtro de categorias")
            categorias_selecionadas = st.multiselect(
                "Selecione as categorias que devem aparecer nos gráficos:",
                options=categorias,
                default=default_categorias,
            )

            if not categorias_selecionadas:
                st.warning("Selecione ao menos uma categoria para exibir os gráficos.")
            else:
                df_plot = df[df["Categoria"].isin(categorias_selecionadas)].copy()

                # =========================
                # AGREGAÇÃO
                # =========================
                agg = df_plot.groupby("Categoria", as_index=False)[valor_col].sum()
                total = agg[valor_col].sum() if agg[valor_col].sum() != 0 else 1
                agg["percentual"] = (agg[valor_col] / total) * 100
                agg = agg.sort_values(valor_col, ascending=False)

                colL, colR = st.columns([1, 1])

                # =========================
                # GRÁFICO – TOTAL (R$)
                # =========================
                with colL:
                    st.markdown("### Total por categoria (R$)")

                    agg["label_valor"] = agg[valor_col].map(
                        lambda x: f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    )

                    fig1 = px.bar(
                        agg,
                        x="Categoria",
                        y=valor_col,
                        text="label_valor",
                    )
                    fig1.update_traces(textposition="outside", cliponaxis=False)
                    fig1.update_layout(
                        yaxis_title="Total (R$)",
                        xaxis_title="",
                        uniformtext_minsize=10,
                        uniformtext_mode="hide",
                    )

                    st.plotly_chart(fig1, use_container_width=True)

                # =========================
                # GRÁFICO – PERCENTUAL (%)
                # =========================
                with colR:
                    st.markdown("### Percentual por categoria (%)")

                    agg["label_pct"] = agg["percentual"].map(lambda x: f"{x:.1f}%")

                    fig2 = px.bar(
                        agg,
                        x="Categoria",
                        y="percentual",
                        text="label_pct",
                    )
                    fig2.update_traces(textposition="outside", cliponaxis=False)
                    fig2.update_layout(
                        yaxis_title="Percentual (%)",
                        xaxis_title="",
                        uniformtext_minsize=10,
                        uniformtext_mode="hide",
                    )

                    st.plotly_chart(fig2, use_container_width=True)

        else:
            st.info("Para o gráfico, garanta que existam as colunas **Categoria** e **valor**.")

st.caption("Dica: no Google Sheets, mantenha a aba 'db' com cabeçalho fixo e colunas estáveis, para o metódo de inserir dados não virar bagunça.")
