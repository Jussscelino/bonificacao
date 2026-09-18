import os
import sqlite3
import hashlib
import csv
import io
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, g, send_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "dados.db")

app = Flask(__name__)
app.secret_key = "troque-esta-chave-secreta-por-uma-aleatoria"

# ============================================================
# CONFIGURAÇÕES DO NEGÓCIO
# ============================================================
PERCENTUAL_PONTOS = 0.01          # 1% do valor vira bonificação
DIAS_VALIDADE_PONTOS = 365        # validade de 1 ano
NOME_CONSUMIDOR_FINAL = "CONSUMIDOR FINAL"
PONTUAR_CONSUMIDOR_FINAL = False  # se True, "CONSUMIDOR FINAL" também ganha pontos

# ============================================================
# BANCO DE DADOS
# ============================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        documento TEXT,
        telefone TEXT,
        criado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS lotes_csv (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome_arquivo TEXT,
        hash_arquivo TEXT UNIQUE,
        total_registros INTEGER DEFAULT 0,
        total_valor REAL DEFAULT 0,
        periodo_inicio TEXT,
        periodo_fim TEXT,
        importado_em TEXT DEFAULT CURRENT_TIMESTAMP,
        ativo INTEGER DEFAULT 1
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS vendas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_venda TEXT UNIQUE,
        cliente_id INTEGER,
        cliente_nome TEXT,
        loja TEXT,
        forma_pagamento TEXT,
        valor_bruto REAL,
        desconto REAL,
        valor_liquido REAL,
        data_venda TEXT,
        lote_id INTEGER,
        pontos_gerados REAL,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id),
        FOREIGN KEY(lote_id) REFERENCES lotes_csv(id)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS movimentos_pontos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER,
        pontos REAL,
        tipo TEXT,
        descricao TEXT,
        data_movimento TEXT DEFAULT CURRENT_TIMESTAMP,
        data_expiracao TEXT,
        venda_id INTEGER,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id)
    )""")

    db.commit()
    db.close()

# ============================================================
# HELPERS
# ============================================================
def hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def to_float(valor_str):
    """Converte '1.234,56' ou '1,50' ou '10.5' em float."""
    if valor_str is None:
        return 0.0
    s = str(valor_str).strip().replace('"', '')
    if not s:
        return 0.0
    # Se tem vírgula, assume padrão brasileiro
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0

def parse_data_hora(s):
    """Aceita 'DD/MM/AAAA HH:MM:SS' ou só data."""
    if not s:
        return datetime.now().strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s = str(s).strip().replace('"', '')
    formatos = [
        ("%d/%m/%Y %H:%M:%S", True),
        ("%d/%m/%Y %H:%M", True),
        ("%d/%m/%Y", False),
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d", False),
    ]
    for fmt, tem_hora in formatos:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    hoje = datetime.now()
    return hoje.strftime("%Y-%m-%d"), hoje.strftime("%Y-%m-%d %H:%M:%S")

def calcular_saldo_cliente(db, cliente_id):
    """Saldo atual, aplicando expiração automática."""
    hoje = datetime.now().strftime("%Y-%m-%d")
    cur = db.cursor()
    cur.execute("""
        UPDATE movimentos_pontos
        SET tipo = 'expiracao'
        WHERE cliente_id = ?
          AND tipo = 'credito'
          AND data_expiracao IS NOT NULL
          AND data_expiracao < ?
    """, (cliente_id, hoje))
    db.commit()

    cur.execute("""
        SELECT COALESCE(SUM(
            CASE WHEN tipo = 'credito' THEN pontos
                 WHEN tipo IN ('resgate','expiracao') THEN -ABS(pontos)
                 ELSE 0 END
        ), 0) AS saldo
        FROM movimentos_pontos
        WHERE cliente_id = ?
    """, (cliente_id,))
    return float(cur.fetchone()["saldo"] or 0)

def obter_ou_criar_cliente(db, nome):
    """Busca por nome exato (case-insensitive) ou cria."""
    cur = db.cursor()
    cur.execute("SELECT id FROM clientes WHERE UPPER(nome) = UPPER(?)", (nome,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("INSERT INTO clientes (nome) VALUES (?)", (nome,))
    return cur.lastrowid

# ============================================================
# LEITOR DO CSV ESPECÍFICO DO SISTEMA
# ============================================================
def ler_csv_vendas(conteudo_bytes):
    """
    Lê o CSV no formato RelVendaPorData.csv:
    "venda";"cliente";"valor_bruto";"desconto";"valor_liquido";"loja";"pagamento";"data_hora"
    Retorna lista de dicionários.
    """
    try:
        texto = conteudo_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = conteudo_bytes.decode("latin-1")

    # Detecta delimitador: se tem ';' usa ';', senão ','
    primeira_linha = texto.splitlines()[0] if texto.splitlines() else ""
    delim = ";" if ";" in primeira_linha else ","

    leitor = csv.reader(io.StringIO(texto), delimiter=delim)
    vendas = []

    for i, linha in enumerate(leitor):
        if not linha or all(not c.strip() for c in linha):
            continue

        # Pula cabeçalho se a primeira célula não for numérica
        if i == 0 and not linha[0].strip().strip('"').replace(".", "").isdigit():
            continue

        # Aceita 8 colunas (formato padrão)
        if len(linha) < 5:
            continue

        numero = linha[0].strip().strip('"')
        cliente = linha[1].strip().strip('"') if len(linha) > 1 else ""

        valor_bruto = to_float(linha[2]) if len(linha) > 2 else 0.0
        desconto    = to_float(linha[3]) if len(linha) > 3 else 0.0
        valor_liq   = to_float(linha[4]) if len(linha) > 4 else valor_bruto - desconto
        loja        = linha[5].strip().strip('"') if len(linha) > 5 else ""
        pagamento   = linha[6].strip().strip('"') if len(linha) > 6 else ""
        data_hora   = linha[7] if len(linha) > 7 else ""

        data_iso, data_hora_iso = parse_data_hora(data_hora)

        vendas.append({
            "numero": numero,
            "cliente": cliente,
            "valor_bruto": valor_bruto,
            "desconto": desconto,
            "valor_liquido": valor_liq,
            "loja": loja,
            "pagamento": pagamento,
            "data": data_iso,
            "data_hora": data_hora_iso,
        })

    return vendas

# ============================================================
# ROTAS
# ============================================================
@app.route("/")
def index():
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT COUNT(*) AS n FROM clientes")
    total_clientes = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(valor_liquido),0) AS v FROM vendas")
    row = cur.fetchone()
    total_vendas = row["n"]
    total_valor = float(row["v"] or 0)

    cur.execute("SELECT COUNT(*) AS n FROM lotes_csv WHERE ativo = 1")
    total_lotes = cur.fetchone()["n"]

    cur.execute("""
        SELECT COALESCE(SUM(
            CASE WHEN tipo='credito' THEN pontos
                 WHEN tipo IN ('resgate','expiracao') THEN -ABS(pontos)
                 ELSE 0 END
        ),0) AS s FROM movimentos_pontos
    """)
    saldo_total = float(cur.fetchone()["s"] or 0)

    cur.execute("SELECT * FROM lotes_csv ORDER BY id DESC LIMIT 20")
    lotes = cur.fetchall()

    # Totais por loja
    cur.execute("""
        SELECT loja, COUNT(*) AS qtd, COALESCE(SUM(valor_liquido),0) AS total
        FROM vendas GROUP BY loja ORDER BY total DESC
    """)
    por_loja = cur.fetchall()

    return render_template("index.html",
                           total_clientes=total_clientes,
                           total_vendas=total_vendas,
                           total_valor=total_valor,
                           total_lotes=total_lotes,
                           saldo_total=saldo_total,
                           lotes=lotes,
                           por_loja=por_loja)

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        arquivo = request.files.get("arquivo")
        if not arquivo or arquivo.filename == "":
            flash("Selecione um arquivo CSV.", "erro")
            return redirect(url_for("upload"))

        conteudo = arquivo.read()
        if not conteudo:
            flash("Arquivo vazio.", "erro")
            return redirect(url_for("upload"))

        hash_arq = hash_bytes(conteudo)
        confirmar = request.form.get("confirmar") == "1"

        db = get_db()
        cur = db.cursor()

        cur.execute("SELECT * FROM lotes_csv WHERE hash_arquivo = ?", (hash_arq,))
        lote_existente = cur.fetchone()

        # CSV idêntico já importado → pergunta antes de substituir
        if lote_existente and not confirmar:
            return render_template("upload.html",
                                   duplicado=True,
                                   lote=lote_existente,
                                   nome_arquivo=arquivo.filename,
                                   conteudo_hex=conteudo.hex())

        # Parse
        vendas = ler_csv_vendas(conteudo)
        if not vendas:
            flash("Nenhuma venda válida encontrada no CSV. "
                  "Verifique se o arquivo está no formato RelVendaPorData.", "erro")
            return redirect(url_for("upload"))

        # Se confirmou substituição, apaga lote antigo e movimentos vinculados
        if lote_existente and confirmar:
            cur.execute("""
                DELETE FROM movimentos_pontos
                WHERE venda_id IN (SELECT id FROM vendas WHERE lote_id = ?)
            """, (lote_existente["id"],))
            cur.execute("DELETE FROM vendas WHERE lote_id = ?", (lote_existente["id"],))
            cur.execute("DELETE FROM lotes_csv WHERE id = ?", (lote_existente["id"],))
            db.commit()

        # Cria novo lote
        datas = sorted(v["data"] for v in vendas)
        cur.execute("""
            INSERT INTO lotes_csv (nome_arquivo, hash_arquivo, periodo_inicio, periodo_fim, ativo)
            VALUES (?, ?, ?, ?, 1)
        """, (arquivo.filename, hash_arq, datas[0], datas[-1],))
        lote_id = cur.lastrowid

        total_reg = 0
        total_val = 0.0
        ignorados = 0
        sem_pontos = 0

        for v in vendas:
            # Venda já existe em outro lote? ignora
            cur.execute("SELECT id FROM vendas WHERE numero_venda = ?", (v["numero"],))
            if cur.fetchone():
                ignorados += 1
                continue

            cliente_nome = v["cliente"].strip()
            pontua = True

            # "CONSUMIDOR FINAL" → não pontua (a menos que configurado)
            if (not PONTUAR_CONSUMIDOR_FINAL and
                    cliente_nome.upper() == NOME_CONSUMIDOR_FINAL):
                pontua = False

            cliente_id = obter_ou_criar_cliente(db, cliente_nome) if pontua else None

            pontos = round(v["valor_liquido"] * PERCENTUAL_PONTOS, 2) if pontua else 0.0

            if pontua:
                expiracao = (datetime.strptime(v["data"], "%Y-%m-%d") +
                             timedelta(days=DIAS_VALIDADE_PONTOS)).strftime("%Y-%m-%d")
            else:
                expiracao = None
                sem_pontos += 1

            cur.execute("""
                INSERT INTO vendas
                (numero_venda, cliente_id, cliente_nome, loja, forma_pagamento,
                 valor_bruto, desconto, valor_liquido, data_venda, lote_id, pontos_gerados)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (v["numero"], cliente_id, cliente_nome, v["loja"], v["pagamento"],
                  v["valor_bruto"], v["desconto"], v["valor_liquido"],
                  v["data"], lote_id, pontos))
            venda_id = cur.lastrowid

            if pontua and pontos > 0:
                cur.execute("""
                    INSERT INTO movimentos_pontos
                    (cliente_id, pontos, tipo, descricao, data_expiracao, venda_id)
                    VALUES (?, ?, 'credito', ?, ?, ?)
                """, (cliente_id, pontos,
                      f"Compra #{v['numero']} — {v['loja']} ({v['data']})",
                      expiracao, venda_id))

            total_reg += 1
            total_val += v["valor_liquido"]

        cur.execute("""UPDATE lotes_csv
                       SET total_registros=?, total_valor=? WHERE id=?""",
                    (total_reg, total_val, lote_id))
        db.commit()

        msg = f"CSV importado: {total_reg} vendas computadas (R$ {total_val:.2f})."
        if ignorados:
            msg += f" {ignorados} vendas já existiam e foram ignoradas."
        if sem_pontos:
            msg += f" {sem_pontos} vendas de '{NOME_CONSUMIDOR_FINAL}' não geraram pontos."
        flash(msg, "ok")
        return redirect(url_for("index"))

    return render_template("upload.html", duplicado=False)

@app.route("/clientes")
def clientes():
    db = get_db()
    cur = db.cursor()
    busca = request.args.get("q", "").strip()

    sql_base = """
        SELECT c.id, c.nome, c.documento, c.telefone,
               COALESCE((SELECT SUM(valor_liquido) FROM vendas v WHERE v.cliente_id=c.id),0) AS total_gasto,
               COALESCE((SELECT COUNT(*)        FROM vendas v WHERE v.cliente_id=c.id),0) AS qtd_compras
        FROM clientes c
    """

    if busca:
        cur.execute(sql_base + """
            WHERE c.nome LIKE ? OR c.telefone LIKE ? OR c.documento LIKE ?
            ORDER BY total_gasto DESC
        """, (f"%{busca}%", f"%{busca}%", f"%{busca}%"))
    else:
        cur.execute(sql_base + " ORDER BY total_gasto DESC")

    linhas = cur.fetchall()
    lista = []
    for c in linhas:
        saldo = calcular_saldo_cliente(db, c["id"])
        lista.append({
            "id": c["id"],
            "nome": c["nome"],
            "documento": c["documento"],
            "telefone": c["telefone"],
            "total_gasto": float(c["total_gasto"] or 0),
            "qtd_compras": c["qtd_compras"],
            "saldo": saldo,
        })

    return render_template("clientes.html", clientes=lista, busca=busca)

@app.route("/cliente/<int:cid>")
def extrato(cid):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM clientes WHERE id = ?", (cid,))
    cliente = cur.fetchone()
    if not cliente:
        flash("Cliente não encontrado.", "erro")
        return redirect(url_for("clientes"))

    cur.execute("""SELECT * FROM movimentos_pontos
                   WHERE cliente_id = ? ORDER BY id DESC""", (cid,))
    movs = cur.fetchall()

    cur.execute("""SELECT * FROM vendas
                   WHERE cliente_id = ? ORDER BY data_venda DESC""", (cid,))
    vendas = cur.fetchall()

    saldo = calcular_saldo_cliente(db, cid)
    total_gasto = sum(float(v["valor_liquido"] or 0) for v in vendas)

    return render_template("extrato.html",
                           cliente=cliente, movimentos=movs, vendas=vendas,
                           saldo=saldo, total_gasto=total_gasto)

@app.route("/cliente/<int:cid>/resgatar", methods=["POST"])
def resgatar(cid):
    db = get_db()
    valor = to_float(request.form.get("valor", "0"))
    saldo = calcular_saldo_cliente(db, cid)

    if valor <= 0:
        flash("Informe um valor maior que zero.", "erro")
        return redirect(url_for("extrato", cid=cid))
    if valor > saldo + 0.001:
        flash(f"Saldo insuficiente. Disponível: R$ {saldo:.2f}", "erro")
        return redirect(url_for("extrato", cid=cid))

    cur = db.cursor()
    cur.execute("""
        INSERT INTO movimentos_pontos (cliente_id, pontos, tipo, descricao)
        VALUES (?, ?, 'resgate', ?)
    """, (cid, valor, f"Resgate em mercadoria — R$ {valor:.2f}"))
    db.commit()

    flash(f"Resgate de R$ {valor:.2f} registrado com sucesso.", "ok")
    return redirect(url_for("extrato", cid=cid))

@app.route("/modelo.csv")
def modelo_csv():
    """Baixa um CSV de exemplo no mesmo formato do seu sistema."""
    conteudo = (
        '"00425";"CONSUMIDOR FINAL";"3,50";"0,00";"3,50";"APOLLO32";"A VISTA";"01/08/2026  11:11:13"\n'
        '"00516";"FRANCISCO GERSON ARCANJO ALVES";"750,00";"0,00";"750,00";"JUSCELINO";"A VISTA";"15/08/2026  10:51:01"\n'
        '"00595";"JOSE EVANEI MARQUES";"55,00";"0,00";"55,00";"APOLLO32";"PIX";"31/08/2026  10:32:37"\n'
    )
    return send_file(io.BytesIO(conteudo.encode("utf-8-sig")),
                     mimetype="text/csv; charset=utf-8",
                     as_attachment=True,
                     download_name="modelo_vendas.csv")

# ============================================================
if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)