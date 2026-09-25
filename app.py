import streamlit as st
import pandas as pd
import sqlite3
import hashlib
import io
from datetime import datetime, timedelta
import re

# =========================================================
# CONFIGURAÇÃO DA PÁGINA
# =========================================================
st.set_page_config(
    page_title="Bonificação de Vendas",
    page_icon="🎁",
    layout="wide"
)

# =========================================================
# BANCO DE DADOS
# =========================================================
DB_NAME = "bonificacao.db"

def get_conn():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY,
            usuario TEXT UNIQUE,
            senha_hash TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS vendas (
            id INTEGER PRIMARY KEY,
            numero_venda TEXT UNIQUE,
            cliente TEXT,
            valor_total REAL,
            bonificacao REAL,
            data_venda TEXT,
            data_upload TEXT,
            mes_referencia TEXT,
            hash_arquivo TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS resgates (
            id INTEGER PRIMARY KEY,
            cliente TEXT,
            valor REAL,
            observacao TEXT,
            data_resgate TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY,
            nome_arquivo TEXT,
            mes_referencia TEXT,
            hash_arquivo TEXT UNIQUE,
            data_upload TEXT
        )
    """)
    # Admin padrão: admin / admin123
    c.execute("SELECT COUNT(*) FROM admin")
    if c.fetchone()[0] == 0:
        senha_hash = hashlib.sha256("admin123".encode()).hexdigest()
        c.execute("INSERT INTO admin (usuario, senha_hash) VALUES (?, ?)",
                  ("admin", senha_hash))
    conn.commit()
    conn.close()

# =========================================================
# FUNÇÕES AUXILIARES
# =========================================================
def hash_arquivo(conteudo_bytes):
    return hashlib.md5(conteudo_bytes).hexdigest()

def parse_data_br(txt):
    """Converte '01/08/2026  11:11:13' para datetime"""
    txt = re.sub(r'\s+', ' ', txt.strip())
    return datetime.strptime(txt, "%d/%m/%Y %H:%M:%S")

def parse_valor_br(txt):
    """Converte '1.234,50' ou '3,50' para float"""
    txt = str(txt).strip().replace(".", "").replace(",", ".")
    return float(txt)

def calcular_saldo_cliente(cliente):
    """Saldo = soma de bonificações válidas - resgates"""
    conn = get_conn()
    hoje = datetime.now()
    um_ano_atras = hoje - timedelta(days=365)
    data_corte = um_ano_atras.strftime("%Y-%m-%d")

    c = conn.cursor()
    # Bonificações válidas (venda dentro do último ano)
    c.execute("""
        SELECT COALESCE(SUM(bonificacao), 0) FROM vendas
        WHERE cliente = ? AND data_venda >= ?
    """, (cliente, data_corte))
    total_bonif = c.fetchone()[0]

    # Resgates feitos
    c.execute("""
        SELECT COALESCE(SUM(valor), 0) FROM resgates WHERE cliente = ?
    """, (cliente,))
    total_resg = c.fetchone()[0]

    conn.close()
    return round(total_bonif - total_resg, 2)

def listar_clientes_com_saldo():
    conn = get_conn()
    hoje = datetime.now()
    um_ano_atras = hoje - timedelta(days=365)
    data_corte = um_ano_atras.strftime("%Y-%m-%d")

    df = pd.read_sql_query("""
        SELECT cliente,
               SUM(bonificacao) as total_ganho,
               COUNT(*) as qtd_compras
        FROM vendas
        WHERE data_venda >= ?
        GROUP BY cliente
        ORDER BY total_ganho DESC
    """, conn, params=(data_corte,))
    conn.close()
    return df

# =========================================================
# LOGIN
# =========================================================
def tela_login():
    st.title("🔐 Área do Administrador")
    st.markdown("Faça login para acessar o sistema de bonificação.")

    with st.form("login_form"):
        usuario = st.text_input("Usuário")
        senha = st.text_input("Senha", type="password")
        submitted = st.form_submit_button("Entrar")
        if submitted:
            conn = get_conn()
            c = conn.cursor()
            senha_hash = hashlib.sha256(senha.encode()).hexdigest()
            c.execute("SELECT * FROM admin WHERE usuario=? AND senha_hash=?",
                      (usuario, senha_hash))
            user = c.fetchone()
            conn.close()
            if user:
                st.session_state["logado"] = True
                st.session_state["usuario"] = usuario
                st.rerun()
            else:
                st.error("❌ Usuário ou senha incorretos.")

    st.info("👤 Usuário padrão: **admin** | Senha: **admin123**")

# =========================================================
# DASHBOARD
# =========================================================
def tela_dashboard():
    st.title("📊 Dashboard")

    conn = get_conn()
    hoje = datetime.now()
    um_ano_atras = hoje - timedelta(days=365)
    data_corte = um_ano_atras.strftime("%Y-%m-%d")

    # Métricas
    total_vendas = pd.read_sql_query(
        "SELECT COALESCE(SUM(valor_total),0) as t, COUNT(*) as q FROM vendas WHERE data_venda >= ?",
        conn, params=(data_corte,)
    ).iloc[0]
    total_bonif = pd.read_sql_query(
        "SELECT COALESCE(SUM(bonificacao),0) as t FROM vendas WHERE data_venda >= ?",
        conn, params=(data_corte,)
    ).iloc[0]["t"]
    total_resg = pd.read_sql_query(
        "SELECT COALESCE(SUM(valor),0) as t FROM resgates", conn
    ).iloc[0]["t"]
    qtd_clientes = pd.read_sql_query(
        "SELECT COUNT(DISTINCT cliente) as t FROM vendas WHERE data_venda >= ?",
        conn, params=(data_corte,)
    ).iloc[0]["t"]
    conn.close()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 Total Vendido (ano)", f"R$ {total_vendas['t']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c2.metric("🎁 Bonificação Gerada", f"R$ {total_bonif:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c3.metric("✅ Total Resgatado", f"R$ {total_resg:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c4.metric("👥 Clientes Ativos", qtd_clientes)

    st.markdown("---")
    st.subheader("🏆 Top 10 Clientes (últimos 12 meses)")
    df = listar_clientes_com_saldo().head(10)
    if df.empty:
        st.info("Nenhuma venda registrada ainda.")
    else:
        df["saldo_atual"] = df["cliente"].apply(calcular_saldo_cliente)
        st.dataframe(df, use_container_width=True, hide_index=True)

# =========================================================
# UPLOAD CSV
# =========================================================
def tela_upload():
    st.title("📤 Upload de Vendas do Mês")
    st.markdown("Faça o upload do arquivo CSV gerado pelo seu sistema de vendas.")

    arquivo = st.file_uploader("Selecione o arquivo CSV", type=["csv"])

    if arquivo is not None:
        conteudo = arquivo.read()
        h = hash_arquivo(conteudo)

        # Verifica duplicidade
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT nome_arquivo, mes_referencia, data_upload FROM uploads WHERE hash_arquivo=?", (h,))
        duplicado = c.fetchone()
        conn.close()

        if duplicado:
            st.warning(f"⚠️ Este arquivo **já foi processado** em {duplicado['data_upload']} "
                       f"(mês: {duplicado['mes_referencia']}).")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("🔄 Substituir dados antigos"):
                    processar_csv(conteudo, arquivo.name, h, substituir=True)
                    st.rerun()
            with col2:
                if st.button("❌ Cancelar"):
                    st.rerun()
            return

        # Pré-visualização
        try:
            df = pd.read_csv(io.BytesIO(conteudo), sep=";", header=None, encoding="utf-8")
        except Exception:
            df = pd.read_csv(io.BytesIO(conteudo), sep=";", header=None, encoding="latin1")

        st.markdown("### 📋 Pré-visualização")
        st.dataframe(df.head(10), use_container_width=True)
        st.info(f"Total de {len(df)} vendas no arquivo.")

        mes_ref = st.text_input("Mês de referência (ex: **Agosto/2026**)",
                                value=datetime.now().strftime("%B/%Y").capitalize())

        if st.button("✅ Confirmar e Processar Upload"):
            processar_csv(conteudo, arquivo.name, h, mes_ref)
            st.rerun()

def processar_csv(conteudo, nome_arquivo, h, mes_ref=None, substituir=False):
    conn = get_conn()
    c = conn.cursor()

    if substituir:
        c.execute("DELETE FROM vendas WHERE hash_arquivo=?", (h,))
        c.execute("DELETE FROM uploads WHERE hash_arquivo=?", (h,))

    if mes_ref is None:
        mes_ref = datetime.now().strftime("%B/%Y").capitalize()

    try:
        df = pd.read_csv(io.BytesIO(conteudo), sep=";", header=None, encoding="utf-8")
    except Exception:
        df = pd.read_csv(io.BytesIO(conteudo), sep=";", header=None, encoding="latin1")

    data_upload = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inseridas = 0
    duplicadas = 0

    for _, row in df.iterrows():
        try:
            numero = str(row[0]).strip()
            cliente = str(row[1]).strip().title()
            valor_total = parse_valor_br(row[4])
            data_venda = parse_data_br(str(row[7])).strftime("%Y-%m-%d")
            bonificacao = round(valor_total * 0.01, 2)

            c.execute("""
                INSERT OR IGNORE INTO vendas
                (numero_venda, cliente, valor_total, bonificacao, data_venda,
                 data_upload, mes_referencia, hash_arquivo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (numero, cliente, valor_total, bonificacao, data_venda,
                  data_upload, mes_ref, h))
            if c.rowcount > 0:
                inseridas += 1
            else:
                duplicadas += 1
        except Exception as e:
            st.error(f"Erro na linha: {row} — {e}")

    c.execute("""
        INSERT OR REPLACE INTO uploads
        (nome_arquivo, mes_referencia, hash_arquivo, data_upload)
        VALUES (?, ?, ?, ?)
    """, (nome_arquivo, mes_ref, h, data_upload))

    conn.commit()
    conn.close()

    st.success(f"✅ **{inseridas}** vendas processadas com sucesso!")
    if duplicadas:
        st.info(f"ℹ️ {duplicadas} vendas já existiam e foram ignoradas.")

# =========================================================
# CONSULTA CLIENTE
# =========================================================
def tela_consulta():
    st.title("🔍 Consulta de Cliente")
    st.markdown("Digite o nome do cliente para ver o extrato completo.")

    conn = get_conn()
    clientes = pd.read_sql_query(
        "SELECT DISTINCT cliente FROM vendas ORDER BY cliente", conn
    )["cliente"].tolist()
    conn.close()

    busca = st.text_input("Nome do cliente")
    if busca:
        clientes_filtrados = [c for c in clientes if busca.upper() in c.upper()]
        if not clientes_filtrados:
            st.warning("Nenhum cliente encontrado.")
            return
        cliente = st.selectbox("Selecione o cliente", clientes_filtrados)
    else:
        cliente = st.selectbox("Selecione o cliente", [""] + clientes)
        if not cliente:
            return

    # Dados do cliente
    conn = get_conn()
    df_vendas = pd.read_sql_query(
        "SELECT * FROM vendas WHERE cliente=? ORDER BY data_venda DESC",
        conn, params=(cliente,)
    )
    df_resg = pd.read_sql_query(
        "SELECT * FROM resgates WHERE cliente=? ORDER BY data_resgate DESC",
        conn, params=(cliente,)
    )
    conn.close()

    saldo = calcular_saldo_cliente(cliente)
    total_gasto = df_vendas["valor_total"].sum() if not df_vendas.empty else 0
    total_bonif = df_vendas["bonificacao"].sum() if not df_vendas.empty else 0
    total_resg = df_resg["valor"].sum() if not df_resg.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 Total Gasto", f"R$ {total_gasto:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c2.metric("🎁 Bonificação Gerada", f"R$ {total_bonif:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c3.metric("✅ Resgatado", f"R$ {total_resg:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c4.metric("🟢 Saldo Atual", f"R$ {saldo:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))

    st.markdown("---")
    tab1, tab2 = st.tabs(["🛒 Histórico de Compras", "🎁 Histórico de Resgates"])

    with tab1:
        if df_vendas.empty:
            st.info("Sem compras registradas.")
        else:
            st.dataframe(df_vendas[["numero_venda", "data_venda", "valor_total",
                                     "bonificacao", "mes_referencia"]],
                         use_container_width=True, hide_index=True)

    with tab2:
        if df_resg.empty:
            st.info("Sem resgates registrados.")
        else:
            st.dataframe(df_resg, use_container_width=True, hide_index=True)

# =========================================================
# RESGATAR PONTOS
# =========================================================
def tela_resgate():
    st.title("🎁 Resgatar Bonificação")

    conn = get_conn()
    clientes = pd.read_sql_query(
        "SELECT DISTINCT cliente FROM vendas ORDER BY cliente", conn
    )["cliente"].tolist()
    conn.close()

    cliente = st.selectbox("Cliente", [""] + clientes)
    if not cliente:
        return

    saldo = calcular_saldo_cliente(cliente)
    st.info(f"💰 Saldo disponível de **{cliente}**: **R$ {saldo:,.2f}**".replace(".", ","))

    if saldo <= 0:
        st.warning("Este cliente não possui saldo para resgatar.")
        return

    valor = st.number_input("Valor a resgatar (R$)", min_value=0.01,
                            max_value=float(saldo), step=0.01, format="%.2f")
    obs = st.text_input("Observação (opcional)", placeholder="Ex: Trocou por 1 produto X")

    if st.button("✅ Confirmar Resgate"):
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO resgates (cliente, valor, observacao, data_resgate)
            VALUES (?, ?, ?, ?)
        """, (cliente, float(valor), obs,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
        st.success(f"✅ Resgate de R$ {valor:,.2f} registrado com sucesso!")
        st.balloons()
        st.rerun()

# =========================================================
# HISTÓRICO DE UPLOADS
# =========================================================
def tela_historico():
    st.title("📜 Histórico de Uploads")
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT nome_arquivo, mes_referencia, data_upload FROM uploads ORDER BY data_upload DESC",
        conn
    )
    conn.close()

    if df.empty:
        st.info("Nenhum upload realizado ainda.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

# =========================================================
# NAVEGAÇÃO
# =========================================================
def main():
    init_db()

    if "logado" not in st.session_state:
        st.session_state["logado"] = False

    if not st.session_state["logado"]:
        tela_login()
        return

    with st.sidebar:
        st.markdown(f"### 👤 {st.session_state.get('usuario', '')}")
        menu = st.radio(
            "Menu",
            ["📊 Dashboard", "📤 Upload CSV", "🔍 Consulta Cliente",
             "🎁 Resgatar Pontos", "📜 Histórico"],
            index=0
        )
        if st.button("🚪 Sair"):
            st.session_state["logado"] = False
            st.rerun()

    if menu.startswith("📊"):
        tela_dashboard()
    elif menu.startswith("📤"):
        tela_upload()
    elif menu.startswith("🔍"):
        tela_consulta()
    elif menu.startswith("🎁"):
        tela_resgate()
    elif menu.startswith("📜"):
        tela_historico()

if __name__ == "__main__":
    main()
